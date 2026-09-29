"""第 5 组 5.1：接通唯一源发送链并保存真实详细回执（统一经 `forward_message` 驱动）。

用例只观察宿主发送能力调用（替身 `ctx.send.calls` 与详细回执注入）、投递账本与
工具结果；真实发送链路由生产实现提供，测试通过替换上下文能力回执来控制
成功/失败/未知，不 mock 内部协作者、不直接调用私有函数。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from delivery_ledger import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryLedger,
    open_ledger,
)

from source_binding import SOURCE_TOKEN_FIELD

from tests.support import (
    TARGET_GROUP_ID,
    FakeClock,
    FailingLedger,
    enabled_config,
    expected_target_session_id,
    forward_node,
    forward_segment,
    image_segment,
    issue_source_call,
    ledger_key,
    make_plugin,
    open_test_ledger,
    prepare_source,
    run_bound_forward,
    text_segment,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent


def test_纯文字源消息经send_text通路发送并保存真实回执(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx, raw_message=[text_segment("源消息正文")])
    ctx.send.text_results = [{"sent": True, "message_id": "m-target-777"}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert result["success"] is True and result["sent"] is True, result
    assert len(ctx.send.calls) == 1, f"未附带评论时只应发送源消息: {ctx.send.calls}"
    call = ctx.send.calls[0]
    assert call["capability"] == "send.text"
    assert call["text"] == "源消息正文", "普通文字保持原形态，不得替换成摘要"
    assert call["stream_id"] == expected_target_session_id(), "必须发送到核对过的目标会话"
    assert call["return_details"] is True, "必须请求详细回执才能拿到真实目标消息 ID"
    assert call["storage_message"] is True, "必须显式开启消息存储"
    assert call["sync_to_maisaka_history"] is True, "必须显式开启 Maisaka 历史同步"
    assert result["target_message_id"] == "m-target-777", result
    assert "m-target-777" not in str(call.get("text", "")), "不得以源正文冒充目标回执"

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-777")

    # 工具只声明正常发送能力：不申请 maisaka.context.append 之类的注入后门
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert "maisaka.context.append" not in manifest["capabilities"]


def test_图片源消息经send_image通路发送可恢复媒体(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx, raw_message=[image_segment(binary_data_base64="aW1hZ2UtYnl0ZXM=")])
    ctx.send.image_results = [{"sent": True, "message_id": "m-target-image"}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert len(ctx.send.calls) == 1, ctx.send.calls
    call = ctx.send.calls[0]
    assert call["capability"] == "send.image"
    assert call["image_base64"] == "aW1hZ2UtYnl0ZXM=", "必须发送可恢复的原始媒体，而不是描述文本"
    assert call["stream_id"] == expected_target_session_id()
    assert call["return_details"] is True
    assert call["storage_message"] is True and call["sync_to_maisaka_history"] is True
    assert result["target_message_id"] == "m-target-image"
    assert open_test_ledger(tmp_path).get(ledger_key()).status == STATUS_SENT


def test_图文混排经send_hybrid保持段序(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    raw_message = [text_segment("一句话"), image_segment()]
    prepare_source(ctx, raw_message=raw_message)
    ctx.send.hybrid_results = [{"sent": True, "message_id": "m-target-mixed"}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert len(ctx.send.calls) == 1, ctx.send.calls
    call = ctx.send.calls[0]
    assert call["capability"] == "send.hybrid"
    assert call["segments"] == raw_message, "混合消息保持原有段序与段内容"
    assert call["stream_id"] == expected_target_session_id()
    assert call["return_details"] is True
    assert call["storage_message"] is True and call["sync_to_maisaka_history"] is True
    assert result["target_message_id"] == "m-target-mixed"


def test_合并转发卡片经send_forward保持节点与作者(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    node = forward_node(
        content=[text_segment("节点原文"), image_segment()],
        user_id="90001",
        user_nickname="甲",
        message_id="m-node-1",
    )
    prepare_source(ctx, raw_message=[forward_segment(node)])
    ctx.send.forward_results = [{"sent": True, "message_id": "m-target-card"}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert len(ctx.send.calls) == 1, ctx.send.calls
    call = ctx.send.calls[0]
    assert call["capability"] == "send.forward"
    assert call["messages"] == [node], "卡片必须按原样发送：节点作者与段内容都不得改写"
    assert call["stream_id"] == expected_target_session_id(), "卡片必须走群会话发送才有路由回执"
    assert call["return_details"] is True
    assert call["storage_message"] is True and call["sync_to_maisaka_history"] is True
    assert result["target_message_id"] == "m-target-card"


def test_多个文本段按换行拼接经send_text通路发送(tmp_path: Path) -> None:
    """一条消息的多个文本段仍是一条普通文本消息：段序不变、以换行拼接、不删改内容。"""

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx, raw_message=[text_segment("第一行"), text_segment("第二行")])
    ctx.send.text_results = [{"sent": True, "message_id": "m-target-join"}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert len(ctx.send.calls) == 1, f"多个文本段只应产生一条消息: {ctx.send.calls}"
    call = ctx.send.calls[0]
    assert call["capability"] == "send.text", "纯文字材料必须走正常文本发送"
    assert call["text"] == "第一行\n第二行", "段序不变、以换行拼接、内容不删改"
    assert call["stream_id"] == expected_target_session_id()
    assert call["return_details"] is True
    assert call["storage_message"] is True and call["sync_to_maisaka_history"] is True
    assert result["target_message_id"] == "m-target-join"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and (record.status, record.target_message_id) == (
        STATUS_SENT,
        "m-target-join",
    )


def test_多张图片经send_hybrid通路发送(tmp_path: Path) -> None:
    """多张图片必须走支持多段的混合通路，逐段原样透传可恢复媒体。"""

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    raw_message = [
        image_segment(binary_data_base64="aW1nLTE="),
        image_segment(binary_data_base64="aW1nLTI=", binary_hash="hash-image-2"),
    ]
    prepare_source(ctx, raw_message=raw_message)
    ctx.send.hybrid_results = [{"sent": True, "message_id": "m-target-album"}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert len(ctx.send.calls) == 1, f"多张图片只应产生一条消息: {ctx.send.calls}"
    call = ctx.send.calls[0]
    assert call["capability"] == "send.hybrid", "多图不能只发第一张或降级成单图通路"
    assert call["segments"] == raw_message, "图片段原样透传（含可恢复媒体数据）"
    assert call["stream_id"] == expected_target_session_id()
    assert call["return_details"] is True
    assert call["storage_message"] is True and call["sync_to_maisaka_history"] is True
    assert result["target_message_id"] == "m-target-album"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and (record.status, record.target_message_id) == (
        STATUS_SENT,
        "m-target-album",
    )


def test_详细回执缺少真实目标ID时按待核实不登记成功(tmp_path: Path) -> None:
    """宿主存在「SENT 但无 external id」的合法分支：bool 不能充当成功证据。"""

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)
    ctx.send.text_results = [{"sent": True, "message_id": None}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_UNKNOWN, result
    assert result["success"] is False, result
    assert result["sent"] is False, "没有真实目标消息 ID 不得声称投递成功"
    assert "待核实" in str(result["reason"]), result
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_UNKNOWN, record
    assert record.target_message_id == "", "空回执 ID 不得写进账本"


def test_宿主未给出成功回执时按待核实不登记成功(tmp_path: Path) -> None:
    """宿主把发送异常与平台失败都归为未成功回执，插件无法区分是否已发出。

    证据：`send_service.py:915-919` 的落库失败发生在平台发送成功之后，异常经
    `:1054-1141` 被吞成 `None` → 能力层 `core.py` 只回报 `sent=False`。因此
    「未成功回执」一律按待核实保守登记，不得降级为可重试的明确失败。
    """

    for label, inject in (
        ("未成功回执", lambda ctx: setattr(ctx.send, "text_result", {"sent": False, "message_id": None})),
        ("能力调用异常", lambda ctx: setattr(ctx.send, "raise_on_text", RuntimeError("RPC 调用失败"))),
    ):
        data_dir = tmp_path / label
        plugin, ctx = make_plugin(data_dir, config=enabled_config())
        prepare_source(ctx)
        inject(ctx)

        result, _ = run_bound_forward(plugin, message_id="m-source-1")

        assert result["status"] == STATUS_UNKNOWN, (label, result)
        assert result["success"] is False and result["sent"] is False, (label, result)
        assert "待核实" in str(result["reason"]), (label, result)
        record = open_test_ledger(data_dir).get(ledger_key())
        assert record is not None and record.status == STATUS_UNKNOWN, (label, record)


def test_契约外成功回执不充当成功证据(tmp_path: Path) -> None:
    """SDK 详细回执经归一后只有 `sent` 与 `message_id`（`context.py:287-291`）。

    只带 `success` 的回执不属于真实发送链的形态；不得按它推定「已发出」，也不得把
    随附的 `message_id` 当成成功证据——一律走保守的待核实分支。
    """

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)
    ctx.send.text_result = {"success": True, "message_id": "m-unnormalized-1"}

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_UNKNOWN, result
    assert result["success"] is False and result["sent"] is False, result
    assert result["target_message_id"] == "", "契约外回执不得作为成功证据写入结果"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_UNKNOWN, record
    assert record.target_message_id == "", "契约外回执 ID 不得写进账本"


def test_多图或混排缺少可恢复媒体时在调用宿主前整条拒绝(tmp_path: Path) -> None:
    """图片媒体数据仅剩空白时预检会放行，发送前必须与单图同一口径整条拒绝。

    单图、多图与图文混排共用同一条本地兜底：调用宿主之前即可确定没有可恢复的
    媒体数据 → 明确未发送，绝不只丢这一段或把不可恢复的段交给宿主。
    """

    cases: list[tuple[str, list[dict[str, Any]]]] = [
        ("多图", [image_segment(binary_data_base64="aW1nLTE="), image_segment(binary_data_base64="   ")]),
        ("混排", [text_segment("正文"), image_segment(binary_data_base64="  ")]),
    ]
    for index, (label, raw_message) in enumerate(cases):
        data_dir = tmp_path / f"missing-media-{index}"
        plugin, ctx = make_plugin(data_dir, config=enabled_config())
        prepare_source(ctx, raw_message=raw_message)

        result, _ = run_bound_forward(plugin, message_id="m-source-1")

        assert result["status"] == STATUS_FAILED, (label, result)
        assert result["success"] is False and result["sent"] is False, (label, result)
        assert "媒体" in str(result["reason"]), (label, result)
        assert "本次未发送任何消息" in str(result["reason"]), (label, result)
        assert ctx.send.calls == [], (label, "调用宿主前即可确定没有可发送的媒体", ctx.send.calls)
        record = open_test_ledger(data_dir).get(ledger_key())
        assert record is not None and record.status == STATUS_FAILED, (label, record)


def test_权限与内容预检通过并完成持久占位后才进入发送(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)
    ctx.send.text_results = [{"sent": True, "message_id": "m-target-order"}]
    arguments = issue_source_call(plugin, message_id="m-source-1")

    async def scenario() -> tuple[Any, dict[str, Any]]:
        ctx.send.hold = asyncio.Event()
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)
        in_flight = open_test_ledger(tmp_path).get(ledger_key())
        assert not task.done(), "发送未完成时工具不得提前返回"
        ctx.send.hold.set()
        return in_flight, await task

    in_flight, result = asyncio.run(scenario())

    assert in_flight is not None and in_flight.status == STATUS_SENDING, (
        "进入发送前必须先持久登记发送中占位"
    )
    assert result["status"] == STATUS_SENT, result
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and (record.status, record.target_message_id) == (
        STATUS_SENT,
        "m-target-order",
    ), "成功回执必须立即落账"


def test_明确未发送的失败不登记成功且账本可重试(tmp_path: Path) -> None:
    """空文本材料在调用宿主前就能确定「没有发出任何消息」→ 明确失败。"""

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx, raw_message=[text_segment("   ")])

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["status"] == STATUS_FAILED, result
    assert result["success"] is False and result["sent"] is False, result
    assert "文本内容" in str(result["reason"]), ("原因需定位到内容本身", result)
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == [], "本地判定为不可发送时不得触碰宿主发送能力"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_FAILED, record


def test_目标群路由来自可信来源账号(tmp_path: Path) -> None:
    """发送目标只由预检产出的可信路由决定，不受模型参数影响。"""

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)

    result, _ = run_bound_forward(
        plugin,
        message_id="m-source-1",
        target_group_id=TARGET_GROUP_ID,
        extra_arguments={"stream_id": "s-forged", "session_id": "s-forged", "platform": "discord"},
    )

    assert result["status"] == STATUS_SENT, result
    assert [call["stream_id"] for call in ctx.send.calls] == [expected_target_session_id()], (
        "模型提供的会话字段不得改变发送目标"
    )


# --------------------------------------------------------------------------- 5.2


def test_评论在源发送成功落账后固定等待半秒(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="附带评论")

    async def scenario() -> tuple[Any, Any, dict[str, Any]]:
        ctx.send.hold = asyncio.Event()
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)
        in_flight = open_test_ledger(tmp_path).get(ledger_key())
        assert clock.sleeps == [], "源发送尚未拿到回执时不得开始评论等待"
        ctx.send.hold.set()
        await wait_until(lambda: clock.sleeps == [0.5])
        waiting = open_test_ledger(tmp_path).get(ledger_key())
        assert len(ctx.send.calls) == 1, "等待期间不得提前发送评论"
        assert not task.done(), "评论尚未完成时工具不得提前返回"
        clock.advance(0.5)
        return in_flight, waiting, await task

    in_flight, waiting, result = asyncio.run(scenario())

    assert in_flight is not None and in_flight.status == STATUS_SENDING, (
        "进入源发送前必须先落发送中占位"
    )
    assert waiting is not None and (waiting.status, waiting.target_message_id) == (
        STATUS_SENT,
        "m-target-src",
    ), "0.5 秒等待必须发生在源发送成功回执落账之后"
    assert clock.sleeps == [0.5], "评论延迟是固定 0.5 秒，不得随机或可配置"
    assert result["status"] == STATUS_SENT, result
    assert len(ctx.send.calls) == 2, f"等待结束后应独立发送评论: {ctx.send.calls}"
    comment_call = ctx.send.calls[1]
    assert comment_call["capability"] == "send.text", "评论必须按普通文本发送"
    assert comment_call["text"] == "附带评论", "评论内容只来自本次 comment 参数"
    assert comment_call["stream_id"] == expected_target_session_id(), "评论发往同一目标群"
    assert comment_call["return_details"] is True
    assert comment_call["storage_message"] is True
    assert comment_call["sync_to_maisaka_history"] is True


def test_评论延迟与内容不受配置或模型参数覆盖(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    comment = "只来自本次参数"

    result, _ = run_bound_forward(
        plugin,
        message_id="m-source-1",
        comment=comment,
        extra_arguments={"delay": 0.01, "comment_delay": 99, "sleep": 0, "random": 0.5},
        clock=clock,
    )

    assert result["status"] == STATUS_SENT, result
    assert clock.sleeps == [0.5], "0.5 秒是代码常量，模型参数不能覆盖"
    config_dump = json.dumps(plugin.config.model_dump(), ensure_ascii=False)
    assert "delay" not in config_dump and "probab" not in config_dump, (
        f"配置面不得出现延迟或概率字段: {config_dump}"
    )
    comment_calls = [
        call
        for call in ctx.send.calls
        if call["capability"] == "send.text" and call["text"] == comment
    ]
    assert len(comment_calls) == 1, "评论只按本次参数发送一次"
    assert {call["capability"] for call in ctx.chat.calls} == {
        "chat.get_all_streams",
        "chat.open_session",
    }, "不得调用目标群麦麦生成评论（只允许已核实的能力面）"


# --------------------------------------------------------------------------- 5.3


def test_空评论或全空白评论只发送源消息且不等待(tmp_path: Path) -> None:
    for index, comment in enumerate(("", "   ", "\n\t ", " 　 ")):
        clock = FakeClock()
        plugin, ctx = make_plugin(
            tmp_path / f"blank-{index}",
            config=enabled_config(),
            clock=clock,
        )
        prepare_source(ctx)
        ctx.send.text_results = [{"sent": True, "message_id": "m-target-src"}]

        result, _ = run_bound_forward(plugin, message_id="m-source-1", comment=comment, clock=clock)

        assert result["status"] == STATUS_SENT, (comment, result)
        assert result["comment_status"] == "", (comment, "未发送评论不得声称评论结果", result)
        assert clock.sleeps == [], (comment, "空白评论不得登记评论等待")
        assert len(ctx.send.calls) == 1, (comment, "空白评论不得发送第二条消息")
        assert result["target_message_id"] == "m-target-src", (comment, result)
        record = open_test_ledger(tmp_path / f"blank-{index}").get(ledger_key())
        assert record is not None and record.comment_status == "", (comment, record)


def test_源发送明确失败时不发送评论且不登记成功(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx, raw_message=[text_segment("   ")])

    result, _ = run_bound_forward(plugin, message_id="m-source-1", comment="不该发出的评论", clock=clock)

    assert result["status"] == STATUS_FAILED, result
    assert result["sent"] is False, result
    assert result["comment_status"] == "", ("明确失败后不得开始评论段", result)
    assert clock.sleeps == [], "源发送未成功时不得登记评论等待"
    assert ctx.send.calls == [], "源发送明确未发送时不得触碰任何发送能力"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and (record.status, record.comment_status) == (STATUS_FAILED, ""), record


def test_源发送结果未知时不发送评论且不登记成功(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [{"sent": False, "message_id": None}]

    result, _ = run_bound_forward(plugin, message_id="m-source-1", comment="不该发出的评论", clock=clock)

    assert result["status"] == STATUS_UNKNOWN, result
    assert result["sent"] is False, result
    assert result["comment_status"] == "", ("回执未知时不得开始评论段", result)
    assert clock.sleeps == [], "源发送结果未知时不得登记评论等待"
    assert len(ctx.send.calls) == 1, "源发送结果未知时不得追加任何发送"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and (record.status, record.comment_status) == (STATUS_UNKNOWN, ""), record


# --------------------------------------------------------------------------- 5.4


def test_评论未完成时返回部分成功并保留源消息回执(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": False, "message_id": None},
    ]

    result, _ = run_bound_forward(plugin, message_id="m-source-1", comment="这条评论发不出去", clock=clock)

    assert result["status"] == "partial", result
    assert result["sent"] is True and result["delivered"] is True, (
        "评论失败不得抹掉源消息已投递的事实",
        result,
    )
    assert result["target_message_id"] == "m-target-src", result
    assert result["comment_status"] == STATUS_UNKNOWN, ("评论回执未知不得伪报成功", result)
    assert "评论" in str(result["reason"]) and "待核实" in str(result["reason"]), result

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), record
    assert record.comment_status == STATUS_UNKNOWN, "评论结果必须附属于源发送记录"

    # 重复调用不重发源消息，也不补发评论
    repeat, _ = run_bound_forward(plugin, message_id="m-source-1", comment="换条评论也不补发", clock=clock)
    assert repeat["status"] == "already_forwarded", repeat
    assert len(ctx.send.calls) == 2, ("重复调用不得重发源消息或补发评论", ctx.send.calls)
    assert clock.sleeps == [0.5], "重复调用不得再登记评论等待"


def test_评论发送前权限撤销时明确未发送(tmp_path: Path) -> None:
    cases: list[tuple[str, dict[str, Any]]] = [
        ("目标白名单撤销", enabled_config(target_groups=())),
        ("总开关关闭", {"plugin": {"enabled": False}}),
    ]
    for label, revoked in cases:
        clock = FakeClock()
        plugin, ctx = make_plugin(tmp_path / label, config=enabled_config(), clock=clock)
        prepare_source(ctx)
        ctx.send.text_results = [
            {"sent": True, "message_id": "m-target-src"},
            {"sent": True, "message_id": "m-should-not-be-sent"},
        ]
        arguments = issue_source_call(plugin, message_id="m-source-1", comment="等待期间被撤销")

        async def scenario() -> dict[str, Any]:
            ctx.send.hold = asyncio.Event()
            task = asyncio.create_task(plugin.forward_message(**arguments))
            await wait_until(lambda: len(ctx.send.calls) == 1)
            ctx.send.hold.set()
            await wait_until(lambda: clock.sleeps == [0.5])
            plugin.set_plugin_config(revoked)
            clock.advance(0.5)
            return await task

        result = asyncio.run(scenario())

        assert result["status"] == "partial", (label, result)
        assert result["sent"] is True and result["target_message_id"] == "m-target-src", (label, result)
        assert result["comment_status"] == STATUS_FAILED, (label, result)
        assert "评论" in str(result["reason"]) and "未发送" in str(result["reason"]), (label, result)
        assert len(ctx.send.calls) == 1, (label, "撤销权限后不得发送评论", ctx.send.calls)
        record = open_test_ledger(tmp_path / label).get(ledger_key())
        assert record is not None
        assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), (label, record)
        assert record.comment_status == STATUS_FAILED, (label, record)


def test_评论发送被取消时保留源消息已发送事实(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="发送中被取消")

    async def scenario() -> None:
        source_hold = asyncio.Event()
        comment_hold = asyncio.Event()
        ctx.send.hold = source_hold
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)
        source_hold.set()
        ctx.send.hold = comment_hold  # 评论发送将悬停在途
        await wait_until(lambda: clock.sleeps == [0.5])
        clock.advance(0.5)
        await wait_until(lambda: len(ctx.send.calls) == 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), (
        "评论发送被取消不得回滚已成功的源消息投递",
        record,
    )
    assert record.comment_status == "", "被取消的评论不得留下成功结果"

    repeat, _ = run_bound_forward(plugin, message_id="m-source-1", comment="取消后不补发", clock=clock)
    assert repeat["status"] == "already_forwarded", repeat
    assert len(ctx.send.calls) == 2, ("取消遗留的投递不得盲目重发或补发评论", ctx.send.calls)


def test_评论结果落盘失败按未知报告且不自动补发(tmp_path: Path) -> None:
    flaky: list[FailingLedger] = []

    def flaky_open(path: Path) -> Any:
        ledger = open_ledger(path, clock=FakeClock())
        assert isinstance(ledger, DeliveryLedger)
        double = FailingLedger(ledger, failing=("record_comment",))
        flaky.append(double)
        return double

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        clock=clock,
        open_ledger=flaky_open,
    )
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]

    result, _ = run_bound_forward(plugin, message_id="m-source-1", comment="结果存不下来", clock=clock)

    assert result["status"] == "partial", result
    assert result["sent"] is True and result["target_message_id"] == "m-target-src", result
    assert result["comment_status"] == STATUS_UNKNOWN, (
        "评论结果未能落盘时不得声称评论结果已知",
        result,
    )
    assert flaky[0].failed_calls == ["record_comment"], flaky[0].failed_calls
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_SENT, record
    assert (record.comment_status, record.comment_message_id) == ("", ""), (
        "落盘失败不得留下评论结果",
        record,
    )

    repeat, _ = run_bound_forward(plugin, message_id="m-source-1", comment="不补发", clock=clock)
    assert repeat["status"] == "already_forwarded", repeat
    assert len(ctx.send.calls) == 2, ("评论结果未落盘也不得自动补发评论", ctx.send.calls)


# --------------------------------------------------------------------------- 5.5

RESULT_FACE_KEYS = {
    "success",
    "status",
    "sent",
    "delivered",
    "already_forwarded",
    "target_message_id",
    "comment_status",
    "comment_message_id",
    "source_message_id",
    "target_group_id",
    "stage",
    "reason",
}
"""工具结果的固定键面（拒绝分支不含材料键，因为没有任何已准备的材料）。"""


def test_工具结果可区分七种调用状态且口径一致(tmp_path: Path) -> None:
    cases: dict[str, dict[str, Any]] = {}

    # 1) 完整成功 / 2) 部分成功（评论回执未知）
    for label, comment, text_results in (
        ("sent", "", [{"sent": True, "message_id": "m-target-src"}]),
        (
            "partial",
            "评论发不出去",
            [{"sent": True, "message_id": "m-target-src"}, {"sent": False, "message_id": None}],
        ),
    ):
        clock = FakeClock()
        plugin, ctx = make_plugin(tmp_path / label, config=enabled_config(), clock=clock)
        prepare_source(ctx)
        ctx.send.text_results = text_results
        cases[label] = run_bound_forward(
            plugin,
            message_id="m-source-1",
            comment=comment,
            clock=clock,
        )[0]

    # 3) 已转发（同键成功记录）
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path / "already", config=enabled_config(), clock=clock)
    prepare_source(ctx)
    run_bound_forward(plugin, message_id="m-source-1", clock=clock)
    cases["already_forwarded"] = run_bound_forward(plugin, message_id="m-source-1", clock=clock)[0]

    # 4) 发送中（同键并发调用）
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path / "sending", config=enabled_config(), clock=clock)
    prepare_source(ctx)
    first_arguments = issue_source_call(plugin, message_id="m-source-1")
    second_arguments = issue_source_call(plugin, message_id="m-source-1")

    async def concurrency() -> dict[str, Any]:
        ctx.send.hold = asyncio.Event()
        in_flight = asyncio.create_task(plugin.forward_message(**first_arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)
        concurrent = await plugin.forward_message(**second_arguments)
        ctx.send.hold.set()
        await in_flight
        return concurrent

    cases["sending"] = asyncio.run(concurrency())

    # 5) 拒绝（目标群不在白名单）
    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path / "rejected",
        config=enabled_config(target_groups=("30003",)),
        clock=clock,
    )
    prepare_source(ctx)
    cases["rejected"] = run_bound_forward(plugin, message_id="m-source-1", clock=clock)[0]

    # 6) 明确失败（本地不可发送的空文本材料）
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path / "failed", config=enabled_config(), clock=clock)
    prepare_source(ctx, raw_message=[text_segment("   ")])
    cases["failed"] = run_bound_forward(plugin, message_id="m-source-1", clock=clock)[0]

    # 7) 结果未知（宿主未返回成功回执）
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path / "unknown", config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [{"sent": False, "message_id": None}]
    cases["unknown"] = run_bound_forward(plugin, message_id="m-source-1", clock=clock)[0]

    for status, result in cases.items():
        assert result["status"] == status, (status, result)
        assert RESULT_FACE_KEYS <= set(result), (status, "结果面必须同形", result)
        assert result["success"] == (status in (STATUS_SENT, "already_forwarded")), (
            status,
            "success 只表示调用完整成功（源消息已投递且评论无未完成项）",
            result,
        )
        assert result["sent"] == result["delivered"], (status, "sent 与 delivered 同义", result)
        assert result["source_message_id"] == "m-source-1", (status, result)
        assert result["target_group_id"] == TARGET_GROUP_ID, (status, result)
        assert str(result["reason"]).strip(), (status, "任何结果都必须带中文原因", result)
        # 只有完整成功与已转发可以声称源消息已确认投递
        assert result["sent"] == (status in (STATUS_SENT, "partial", "already_forwarded")), (
            status,
            result,
        )
        # 尚未完成的调用不得提前声称源消息已发送
        if status in ("sending", "rejected", "failed", "unknown"):
            assert result["sent"] is False, (status, result)
            assert result["target_message_id"] == "", (status, result)
        # 错误阶段：无故障的调用为空串，其余必须定位到具体阶段
        if status in (STATUS_SENT, "sending", "already_forwarded"):
            assert result["stage"] == "", (status, result)
        else:
            assert result["stage"] in (
                "rejected",
                "source_send",
                "source_persist",
                "comment_send",
                "comment_persist",
            ), (status, result)
        # 评论状态只对完整成功为 sent；其余不得伪报评论已发送
        if status != STATUS_SENT:
            assert result["comment_status"] != STATUS_SENT, (status, result)
        # 材料键只在预检通过后出现
        assert ("material_shape" in result) == (status != "rejected"), (status, result)
        assert ("send_kind" in result) == (status != "rejected"), (status, result)

    assert cases["sent"]["comment_status"] == "", cases["sent"]
    assert cases["sent"]["target_message_id"] == "m-target-src", cases["sent"]
    assert cases["partial"]["comment_status"] == STATUS_UNKNOWN, cases["partial"]
    assert cases["partial"]["target_message_id"] == "m-target-src", cases["partial"]
    assert cases["already_forwarded"]["target_message_id"] == "text-1", cases["already_forwarded"]
    assert cases["failed"]["stage"] == "source_send", cases["failed"]
    assert cases["unknown"]["stage"] == "source_send", cases["unknown"]
    assert cases["rejected"]["stage"] == "rejected", cases["rejected"]
    assert "白名单" in str(cases["rejected"]["reason"]), cases["rejected"]


def test_账本不可用与拒绝结果同形且不声称发送(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path / "blocked",
        config=enabled_config(),
        clock=clock,
        storage_fault=True,
    )
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", clock=clock)

    assert RESULT_FACE_KEYS <= set(result), result
    assert result["status"] == "ledger_unavailable", result
    assert result["success"] is False and result["sent"] is False, result
    assert result["stage"] == "source_persist", result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == [], "账本不可用时不得发送"


def test_发送与评论未完成时不提前返回全部成功(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="等待真实结果")

    async def scenario() -> dict[str, Any]:
        source_hold = asyncio.Event()
        comment_hold = asyncio.Event()
        ctx.send.hold = source_hold
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)
        assert not task.done(), "源发送在途时不得提前返回"
        source_hold.set()
        ctx.send.hold = comment_hold
        await wait_until(lambda: clock.sleeps == [0.5])
        assert not task.done(), "评论等待期间不得提前返回"
        clock.advance(0.5)
        await wait_until(lambda: len(ctx.send.calls) == 2)
        assert not task.done(), "评论发送在途时不得提前返回全部成功"
        comment_hold.set()
        return await task

    result = asyncio.run(scenario())

    assert result["status"] == STATUS_SENT, result
    assert result["comment_status"] == STATUS_SENT, result
    assert result["comment_message_id"] == "m-target-comment", result


# --------------------------------------------------------------------------- 5.6


def _run_failure_case(
    tmp_path: Path,
    label: str,
    *,
    comment: str = "",
    raw_message: list[dict[str, Any]] | None = None,
    text_results: list[Any] | None = None,
    config: dict[str, Any] | None = None,
    open_ledger: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], Any, Any]:
    """驱动一个失败场景，返回（工具结果, 工具参数, 上下文, 时钟）。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path / label,
        config=config or enabled_config(),
        clock=clock,
        open_ledger=open_ledger,
    )
    prepare_source(ctx, raw_message=raw_message)
    if text_results is not None:
        ctx.send.text_results = text_results
    result, arguments = run_bound_forward(
        plugin,
        message_id="m-source-1",
        comment=comment,
        clock=clock,
    )
    return result, arguments, ctx, clock


def test_各阶段失败均可定位且不输出凭证(tmp_path: Path) -> None:
    """拒绝、源发送、持久化、评论四个阶段的失败都要能定位，且不泄露凭据。"""

    def flaky_open(path: Path) -> Any:
        ledger = open_ledger(path, clock=FakeClock())
        assert isinstance(ledger, DeliveryLedger)
        return FailingLedger(ledger, failing=("mark_sent",))

    cases: list[tuple[str, dict[str, Any], str, int]] = [
        ("拒绝", {"config": enabled_config(target_groups=("30003",))}, "rejected", 0),
        ("源发送", {"text_results": [{"sent": False, "message_id": None}]}, "source_send", 1),
        ("持久化", {"open_ledger": flaky_open}, "source_persist", 1),
        (
            "评论",
            {
                "comment": "评论发不出去",
                "text_results": [
                    {"sent": True, "message_id": "m-target-src"},
                    {"sent": False, "message_id": None},
                ],
            },
            "comment_send",
            2,
        ),
    ]

    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert "maisaka.context.append" not in manifest["capabilities"], (
        "不得申请额外上下文注入通路（不生成摘要或评论副本）"
    )

    for label, kwargs, expected_stage, expected_sends in cases:
        result, arguments, ctx, _ = _run_failure_case(tmp_path, label, **kwargs)

        assert result["stage"] == expected_stage, (label, result)
        assert result["source_message_id"] == "m-source-1", (label, result)
        assert result["target_group_id"] == TARGET_GROUP_ID, (label, result)
        assert str(result["reason"]).strip(), (label, "必须有中文原因", result)

        # 凭证（一次性来源凭据）绝不出现在工具结果或日志里
        token = str(arguments[SOURCE_TOKEN_FIELD])
        assert token, (label, "本用例需要真实凭据才谈得上不泄露")
        rendered = json.dumps(result, ensure_ascii=False)
        assert token not in rendered and SOURCE_TOKEN_FIELD not in rendered, (label, result)
        records = json.dumps(ctx.logger.records, ensure_ascii=False)
        assert token not in records and SOURCE_TOKEN_FIELD not in records, (label, ctx.logger.records)

        # 日志必须能定位源消息、目标群与失败阶段
        assert "m-source-1" in records, (label, "日志必须能定位源消息", ctx.logger.records)
        assert TARGET_GROUP_ID in records, (label, "日志必须能定位目标群", ctx.logger.records)
        assert expected_stage in records, (label, "日志必须能定位阶段", ctx.logger.records)

        # 失败发送不得被记为成功历史
        record = open_test_ledger(tmp_path / label).get(ledger_key())
        if expected_stage == "comment_send":
            assert record is not None and record.status == STATUS_SENT, (label, record)
            assert record.comment_status != STATUS_SENT, (label, record)
        else:
            assert record is None or record.status != STATUS_SENT, (label, record)

        # 只走已核实的发送通路，且评论只有一条（不生成评论副本）
        assert len(ctx.send.calls) == expected_sends, (label, ctx.send.calls)
        assert all(
            call["capability"] in ("send.text", "send.image", "send.hybrid", "send.forward")
            for call in ctx.send.calls
        ), (label, ctx.send.calls)


def test_成功结果不产生额外失败日志(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1")

    assert result["success"] is True, result
    assert ctx.logger.texts("warning") == [], ctx.logger.records
    assert ctx.logger.texts("error") == [], ctx.logger.records
