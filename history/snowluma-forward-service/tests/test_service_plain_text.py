"""4.4 普通文字与提及文本化。

判据（tasks.md 4.4 与 design D3、cross-group-forward「支持消息类型与完整性」）：

- 个人/全体提及使用原有文本呈现（`［提及：显示名称（对象标识）］` / `［提及：全体成员］`），
  名称与标识只来自源材料；缺显示名只用对象标识，缺对象整条拒绝；
- 保留段顺序与原换行；转换不新增换行、不生成实际 `at` 段；
- 不支持段（回复、文件、语音等）混排时整条拒绝，不静默丢弃后继续发送。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from service.gateway import PrepareRejected, PreparedSource, SnowLumaGateway
from service.materials import (
    ERROR_SOURCE_MENTION_UNCONFIRMED,
    ERROR_SOURCE_SEGMENT_UNSUPPORTED,
    PrepareRejected as MaterialRejected,
    build_source_material,
)
from shared.send_steps import SourceMaterial, SourceSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
    store_source_message,
)


def _gateway(onebot: FakeOneBot) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=BOUND_ACCOUNT_ID, clock=FakeClock())


def _prepare(onebot: FakeOneBot) -> PreparedSource | PrepareRejected:
    return asyncio.run(
        _gateway(onebot).prepare_source(
            source_group_id=SOURCE_GROUP_ID,
            source_message_id=SOURCE_MESSAGE_ID,
            target_group_id=TARGET_GROUP_ID,
        )
    )


def _with_message(message: list[Mapping[str, Any]]) -> FakeOneBot:
    onebot = FakeOneBot()
    store_source_message(
        onebot, message_id=SOURCE_MESSAGE_ID, group_id=SOURCE_GROUP_ID, message=message
    )
    return onebot


def _texts(prepared: PreparedSource) -> list[str]:
    return [str(segment["data"]["text"]) for segment in prepared.material.segments]


def test_个人提及与相邻文字归并成普通文本() -> None:
    onebot = _with_message(
        [
            {"type": "text", "data": {"text": "请 "}},
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " 查看"}},
            {"type": "text", "data": {"text": "第二段\n换行"}},
        ]
    )

    prepared = _prepare(onebot)

    assert isinstance(prepared, PreparedSource), prepared
    assert prepared.material.kind == "text"
    assert prepared.material.segments == (
        {"type": "text", "data": {"text": "请 ［提及：12345］ 查看"}},
        {"type": "text", "data": {"text": "第二段\n换行"}},
    ), "紧邻提及的文本归并成一段；源消息本来的分段边界不被跨过"


def test_全体成员与缺省显示名的提及文本() -> None:
    all_mention = _prepare(_with_message([{"type": "at", "data": {"qq": "all"}}]))
    assert isinstance(all_mention, PreparedSource), all_mention
    assert _texts(all_mention) == ["［提及：全体成员］"], "全体成员只表达含义"

    personal = _prepare(_with_message([{"type": "at", "data": {"qq": "34567"}}]))
    assert isinstance(personal, PreparedSource), personal
    assert _texts(personal) == ["［提及：34567］"], "缺显示名时只用源对象标识，不虚构昵称"

    adjacent = _prepare(
        _with_message(
            [{"type": "at", "data": {"qq": "all"}}, {"type": "at", "data": {"qq": "34567"}}]
        )
    )
    assert isinstance(adjacent, PreparedSource), adjacent
    assert _texts(adjacent) == ["［提及：全体成员］［提及：34567］"], (
        "相邻文本片段（含提及转换）沿用插件侧同一归并语义：不新增分隔符"
    )


def test_显示名称只来自源材料并压平控制字符() -> None:
    onebot = _with_message(
        [{"type": "at", "data": {"qq": "34567", "card": "群名片\n名\t字"}}]
    )
    prepared = _prepare(onebot)
    assert isinstance(prepared, PreparedSource), prepared
    assert _texts(prepared) == ["［提及：群名片 名 字（34567）］"], "控制字符压平，不新增换行"

    # 与插件侧（宿主形态）转换口径一致：格式只有一处契约，不因服务端另写一套而漂移
    from send_material import _mention_display_text  # type: ignore[attr-defined]

    host_text = _mention_display_text(
        {"type": "at", "data": {"target_user_id": "34567", "target_user_cardname": "群名片\n名\t字"}},
        "源消息第 1 段",
    )
    assert _texts(prepared) == [host_text], (host_text, _texts(prepared))


def test_提及呈现跨实现覆盖全体个人无名缺对象与控制字符() -> None:
    """服务端与插件侧的提及呈现必须逐字一致（design D9：格式只有一处契约）。"""

    from send_material import _mention_display_text  # type: ignore[attr-defined]

    accepted: list[tuple[str, dict[str, Any], dict[str, Any], str]] = [
        ("全体成员", {"qq": "all"}, {"target_user_id": "all"}, "［提及：全体成员］"),
        ("缺显示名", {"qq": "34567"}, {"target_user_id": "34567"}, "［提及：34567］"),
        (
            "群名片优先且压平控制字符",
            {"qq": "34567", "card": "群名片\n名\t字"},
            {"target_user_id": "34567", "target_user_cardname": "群名片\n名\t字"},
            "［提及：群名片 名 字（34567）］",
        ),
        (
            "昵称回退",
            {"qq": "34567", "nickname": "昵称"},
            {"target_user_id": "34567", "target_user_nickname": "昵称"},
            "［提及：昵称（34567）］",
        ),
        (
            "空群名片回退昵称",
            {"qq": "34567", "card": "   ", "nickname": "昵称"},
            {"target_user_id": "34567", "target_user_cardname": "   ", "target_user_nickname": "昵称"},
            "［提及：昵称（34567）］",
        ),
    ]
    for label, service_data, host_data, expected in accepted:
        prepared = build_source_material([{"type": "at", "data": service_data}])
        assert isinstance(prepared, SourceMaterial), (label, prepared)
        service_text = str(prepared.segments[0]["data"]["text"])
        host_text = _mention_display_text(
            {"type": "at", "data": host_data}, "源消息第 1 段"
        )
        assert isinstance(host_text, str), (label, host_text)
        assert service_text == host_text == expected, (label, service_text, host_text, expected)

    rejected: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("缺对象", {}, {}),
        ("空对象", {"qq": ""}, {"target_user_id": ""}),
        ("空白对象", {"qq": "   "}, {"target_user_id": "   "}),
    ]
    for label, service_data, host_data in rejected:
        prepared = build_source_material([{"type": "at", "data": service_data}])
        assert isinstance(prepared, MaterialRejected), (label, prepared)
        assert prepared.error == ERROR_SOURCE_MENTION_UNCONFIRMED, (label, prepared)
        host_outcome = _mention_display_text(
            {"type": "at", "data": host_data}, "源消息第 1 段"
        )
        assert not isinstance(host_outcome, str), (label, host_outcome)


def test_提及对象不可确认整条拒绝() -> None:
    cases: list[Any] = [
        {"type": "at", "data": {}},
        {"type": "at", "data": {"qq": ""}},
        {"type": "at", "data": {"qq": "   "}},
        {"type": "at", "data": {"qq": True}},
        {"type": "at", "data": {"qq": ["12345"]}},
        {"type": "at", "data": {"qq": 0}},
        {"type": "at", "data": {"qq": -1}},
        {"type": "at", "data": {"qq": "0"}},
        {"type": "at", "data": {"qq": "-5"}},
        {"type": "at", "data": {"qq": "12.5"}},
        {"type": "at", "data": {"qq": "abc"}},
        {"type": "at", "data": "不是映射结构"},
        {"type": "at"},
    ]
    for segment in cases:
        onebot = _with_message([{"type": "text", "data": {"text": "正文"}}, segment])

        prepared = _prepare(onebot)

        assert isinstance(prepared, PrepareRejected), (segment, prepared)
        assert prepared.error == ERROR_SOURCE_MENTION_UNCONFIRMED, (segment, prepared)
        assert onebot.send_count == 0, "提及对象不可确认必须在发送前整条拒绝"


def test_转换不新增换行也不生成实际提及段() -> None:
    onebot = _with_message(
        [
            {"type": "text", "data": {"text": "第一行\n"}},
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": "\n第二行"}},
        ]
    )

    prepared = _prepare(onebot)

    assert isinstance(prepared, PreparedSource), prepared
    assert "".join(_texts(prepared)).count("\n") == 2, "原换行保留，转换不新增换行"
    assert all(segment["type"] == "text" for segment in prepared.material.segments)
    assert not any(segment["type"] == "at" for segment in prepared.material.segments), (
        "不得生成实际 at 段（不向目标群传播提醒）"
    )


def test_不支持段混排整条拒绝不静默丢弃() -> None:
    cases: list[tuple[str, list[Mapping[str, Any]]]] = [
        ("回复", [{"type": "text", "data": {"text": "正文"}}, {"type": "reply", "data": {"id": "1"}}]),
        ("文件", [{"type": "file", "data": {"file": "/tmp/x.zip"}}]),
        ("语音", [{"type": "record", "data": {"file": "voice.amr"}}]),
        ("视频", [{"type": "video", "data": {"file": "v.mp4"}}]),
        ("未知包装", [{"type": "dict", "data": {"type": "forward"}}]),
    ]
    for label, message in cases:
        onebot = _with_message(message)

        prepared = _prepare(onebot)

        assert isinstance(prepared, PrepareRejected), (label, prepared)
        assert prepared.error == ERROR_SOURCE_SEGMENT_UNSUPPORTED, (label, prepared)
        assert onebot.send_count == 0, (label, "不支持的段不得被丢弃后继续发送")


def test_文字材料按原顺序真实发送且改写为普通文本() -> None:
    onebot = _with_message(
        [
            {"type": "text", "data": {"text": "请 "}},
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " 查看"}},
        ]
    )
    gateway = _gateway(onebot)
    prepared = _prepare(onebot)
    assert isinstance(prepared, PreparedSource), prepared

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

    assert result.status == "sent", result
    assert onebot.send_count == 1
    assert onebot.send_calls[0].detail["message"] == [
        {"type": "text", "data": {"text": "请 ［提及：12345］ 查看"}}
    ], "按普通文字段发送：不重放原消息、不携带实际提及段"
