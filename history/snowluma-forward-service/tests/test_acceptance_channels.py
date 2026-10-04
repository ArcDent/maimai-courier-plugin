"""第 7.1 组跨进程验收：贯通全部消息类型与两种调用入口（真实服务链）。

共同 seam：真实插件公共工具 `forward_message` → 真实客户端 → 真实服务（真实路由、
真实协调器、真实账本）→ 模拟 OneBot；主动判断入口走真实 `chat.receive.after_process`
观察 Hook 与 `maisaka.proactive.trigger`。插件↔服务端的 HTTP 请求走**内存传输**
（`ServiceAppTransport`，不经真实套接字）；真实子进程 + 真实回环套接字的跨进程覆盖
见 `tests/test_acceptance_processes.py`。每条场景经 `tests.acceptance_support` 记录
输入、源/评论发送次数、权威账本与工具结果，不只断言布尔返回。

验收覆盖：文字、单图、多图、图文、普通提及、带内部回复/提及的原卡片与空/非空评论；
原卡片只发一次资源引用；评论为独立文本且顺序/间隔正确；群友请求与 Planner 决定都走
同一工具；Planner 沉默零发送；目标冷会话不额外唤起模型。全部为合成数据，不连接真实群。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from delivery_ledger import STATUS_SENT

from tests.acceptance_support import (
    assert_report_consistent,
    comment_sends_of,
    rendered,
    scenario_report,
    sequence_texts,
)
from tests.client_support import DEFAULT_RESOURCE_ID, make_client_service
from tests.service_support import SOURCE_GROUP_ID, STAGE_COMMENT, STAGE_PLATFORM_COMMIT
from tests.support import (
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    at_segment,
    drive_observe_hook,
    forward_node,
    forward_segment,
    image_segment,
    merged_forward_message,
    run_bound_forward,
    text_segment,
    unexpanded_forward_segment,
)

MENTION_USER_ID = "30001"
"""合成提及对象标识（刻意不同于群号，避免与路由断言混淆）。"""


def asyncio_run(coroutine: Any) -> Any:
    """在同步用例里驱动一次异步设施（命名避免遮蔽标准库导入）。"""

    return asyncio.run(coroutine)


def _tool_status(report: Any) -> str:
    return str(report.tool.get("status") or "")


def _sends_json(fixture: Any) -> str:
    """模拟网关实际收到的全部发送（含阶段），用于「内部材料不得外泄」断言。"""

    return json.dumps(
        [
            {"stage": call.detail.get("stage"), "target": call.target, "message": call.detail.get("message")}
            for call in fixture.onebot.send_calls
        ],
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- 两种调用入口


def test_群友显式请求经真实服务发送文字并保存真实回执(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path, raw_message=[text_segment("群友请求的正文")])

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    record = fixture.record()
    report = scenario_report(
        fixture,
        label="显式工具调用·文字·空评论",
        tool=result,
        input=[text_segment("群友请求的正文")],
        record=record,
    )

    assert_report_consistent(report)
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1, report.summary()
    assert report.comment_send_count == 0, "空评论不得发送第二条消息"
    assert report.source_sends[0] == ({"type": "text", "data": {"text": "群友请求的正文"}},), report.summary()
    assert record is not None and record["status"] == STATUS_SENT, report.summary()
    assert record["target_message_id"] == result["target_message_id"], report.summary()
    assert report.ledger_rows and report.ledger_rows[0][1] == STATUS_SENT, report.summary()
    assert fixture.clock.sleeps == [], "空评论不得登记评论等待"
    assert fixture.old_send_calls() == [], "新投递路径不得触碰宿主旧发送通道"
    fixture.close()


def test_Planner决定经自动入口后走同一工具且评论独立延迟发送(tmp_path: Path) -> None:
    """自动入口只唤起一次判断；Planner 决定后仍经同一 `forward_message` 发送。"""

    fixture = make_client_service(tmp_path, raw_message=[text_segment("待分享的正文")])
    payload = merged_forward_message(
        message_id="m-source-1",
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    asyncio_run(
        drive_observe_hook(
            fixture.plugin,
            fixture.clock,
            payload,
            until=lambda: bool(fixture.ctx.maisaka.proactive.calls),
        )
    )
    proactive = list(fixture.ctx.maisaka.proactive.calls)
    sends_after_trigger = comment_sends_of(fixture)

    result, _ = run_bound_forward(
        fixture.plugin,
        message_id="m-source-1",
        comment="Planner 决定的评论",
        clock=fixture.clock,
    )
    record = fixture.record()
    report = scenario_report(
        fixture,
        label="主动判断·文字·非空评论",
        tool=result,
        input=[text_segment("待分享的正文")],
        record=record,
    )

    assert_report_consistent(report)
    assert len(proactive) == 1, ("自动入口只提供一次判断机会", proactive)
    assert proactive[0]["stream_id"] == SOURCE_SESSION_ID, proactive
    assert proactive[0]["metadata"]["source_message_id"] == "m-source-1", proactive
    assert sends_after_trigger == (), "Planner 沉默之前不得有任何评论发送"
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1, report.summary()
    assert report.comment_send_count == 1, report.summary()
    assert report.source_sends[0] == ({"type": "text", "data": {"text": "待分享的正文"}},), report.summary()
    assert report.comment_sends == ("Planner 决定的评论",), report.summary()
    assert fixture.clock.sleeps == [0.5], "评论必须在源成功后固定等待 0.5 秒"
    assert record is not None and record["comment_status"] == STATUS_SENT, report.summary()
    assert fixture.old_send_calls() == []

    # 目标冷会话不额外唤起模型：只读取会话表，不打开目标群会话、不向目标群唤起。
    capabilities = [call["capability"] for call in fixture.ctx.chat.calls]
    assert set(capabilities) <= {"chat.get_all_streams"}, capabilities
    assert all(call["stream_id"] == SOURCE_SESSION_ID for call in fixture.ctx.maisaka.proactive.calls), (
        "只向源群提供判断机会，从不唤起目标群",
        fixture.ctx.maisaka.proactive.calls,
    )
    fixture.close()


def test_Planner沉默时自动入口零发送(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    payload = merged_forward_message(
        message_id="m-source-1",
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    asyncio_run(
        drive_observe_hook(
            fixture.plugin,
            fixture.clock,
            payload,
            until=lambda: bool(fixture.ctx.maisaka.proactive.calls),
        )
    )

    assert len(fixture.ctx.maisaka.proactive.calls) == 1, "恰唤起一次判断"
    assert fixture.onebot.send_count == 0, "只唤起判断，不发送任何消息"
    assert fixture.onebot.send_calls == [], fixture.onebot.send_calls
    assert fixture.record() is None, "未调用工具就不得留下任何投递记录"
    assert fixture.transport.count("/prepare") == 0, "候选唤起不得进入发送链的准备接口"
    assert fixture.old_send_calls() == []
    fixture.close()


# --------------------------------------------------------------------------- 全部消息类型


def test_单图消息发送可恢复媒体且空评论不等待(tmp_path: Path) -> None:
    fixture = make_client_service(
        tmp_path,
        raw_message=[image_segment(binary_data_base64="aW1hZ2UtYnl0ZXM=")],
    )

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    report = scenario_report(
        fixture,
        label="单图·空评论",
        tool=result,
        input="image(binary)",
        record=fixture.record(),
    )

    assert_report_consistent(report)
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1 and report.comment_send_count == 0, report.summary()
    assert report.source_sends[0] == ({"type": "image", "data": {"url": "https://cdn.example.invalid/media.png"}},), (
        report.summary()
    )
    assert fixture.clock.sleeps == []
    assert fixture.old_send_calls() == []
    fixture.close()


def test_多图消息在同一发送里保持原顺序(tmp_path: Path) -> None:
    fixture = make_client_service(
        tmp_path,
        raw_message=[
            image_segment(binary_data_base64="aW1nLTE="),
            image_segment(binary_data_base64="aW1nLTI=", binary_hash="hash-image-2"),
            image_segment(binary_data_base64="aW1nLTM=", binary_hash="hash-image-3"),
        ],
    )

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    report = scenario_report(fixture, label="多图·空评论", tool=result, input="3 张图片", record=fixture.record())

    assert_report_consistent(report)
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1, "多张图片必须放在同一条消息里"
    urls = [segment["data"]["url"] for segment in report.source_sends[0]]
    assert urls == [
        "https://cdn.example.invalid/media.png",
        "https://cdn.example.invalid/media-2.png",
        "https://cdn.example.invalid/media-3.png",
    ], report.summary()
    fixture.close()


def test_图文混排与普通提及保持原顺序并文本化(tmp_path: Path) -> None:
    raw = [
        text_segment("配文"),
        at_segment(MENTION_USER_ID, nickname="某群友"),
        image_segment(),
        text_segment("结尾"),
    ]
    fixture = make_client_service(tmp_path, raw_message=raw)

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    report = scenario_report(fixture, label="图文混排+个人提及", tool=result, input=raw, record=fixture.record())

    assert_report_consistent(report)
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1 and report.comment_send_count == 0, report.summary()
    assert report.source_sends[0] == (
        {"type": "text", "data": {"text": f"配文［提及：某群友（{MENTION_USER_ID}）］"}},
        {"type": "image", "data": {"url": "https://cdn.example.invalid/media.png"}},
        {"type": "text", "data": {"text": "结尾"}},
    ), report.summary()
    assert all(segment.get("type") != "at" for segments in report.source_sends for segment in segments), (
        "提及只作文本呈现，不生成实际 at 段"
    )
    assert sequence_texts(report) == (f"配文［提及：某群友（{MENTION_USER_ID}）］", "结尾"), report.summary()
    fixture.close()


def test_原卡片带内部回复与提及只发一次资源引用且评论独立(tmp_path: Path) -> None:
    node = forward_node(
        content=[
            {"type": "reply", "data": {"target_message_id": "m-inner-reply"}},
            text_segment("内部正文"),
            at_segment("all"),
            at_segment(MENTION_USER_ID, nickname="内部群友"),
            image_segment(),
        ],
        user_id="90001",
        user_nickname="甲",
        message_id="m-node-1",
    )
    raw = [forward_segment(node)]
    fixture = make_client_service(tmp_path, raw_message=raw)

    result, _ = run_bound_forward(
        fixture.plugin,
        message_id="m-source-1",
        comment="卡片评论",
        clock=fixture.clock,
    )
    report = scenario_report(
        fixture,
        label="原卡片（内部回复/提及）·非空评论",
        tool=result,
        input="forward_card(node: reply+at+at+image)",
        record=fixture.record(),
    )

    assert_report_consistent(report)
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1, "原卡片只发一次资源引用"
    assert report.comment_send_count == 1, "评论是独立的一条文本消息"
    assert report.source_sends[0] == ({"type": "forward", "data": {"id": DEFAULT_RESOURCE_ID}},), report.summary()
    assert report.comment_sends == ("卡片评论",), report.summary()
    assert fixture.clock.sleeps == [0.5], "评论在源成功落账后固定等待 0.5 秒"
    assert report.ledger_rows and report.ledger_rows[0][1] == STATUS_SENT, report.summary()

    # 卡片内部材料既不进入发送段，也不由客户端提交给服务端。
    sends = _sends_json(fixture)
    prepare_body = json.dumps(fixture.transport.body_of("/v1/forwards/prepare"), ensure_ascii=False)
    for internal in ("m-inner-reply", "内部正文", "全体成员", "内部群友", "90001"):
        assert internal not in sends, ("卡片内部材料不得进入发送", internal, sends)
        assert internal not in prepare_body, ("卡片内部材料不得由客户端提交", internal, prepare_body)
    assert sends.count(DEFAULT_RESOURCE_ID) == 1, ("资源引用只能出现一次", sends)
    assert DEFAULT_RESOURCE_ID not in prepare_body, ("资源引用不得由客户端提交", prepare_body)
    assert fixture.old_send_calls() == []
    fixture.close()


def test_未展开原卡片保持资源引用且评论顺序正确(tmp_path: Path) -> None:
    """宿主没能展开节点时（`dict` 包装）仍按原资源引用发送，不受内部材料影响。"""

    raw = [
        unexpanded_forward_segment(
            nodes=[
                forward_node(
                    content=[at_segment("all"), text_segment("未展开内部正文")],
                    message_id="m-unexpanded-node",
                )
            ]
        )
    ]
    fixture = make_client_service(tmp_path, raw_message=raw)

    result, _ = run_bound_forward(
        fixture.plugin, message_id="m-source-1", comment="未展开卡片评论", clock=fixture.clock
    )
    report = scenario_report(
        fixture,
        label="未展开原卡片·非空评论",
        tool=result,
        input="dict-wrapped forward card",
        record=fixture.record(),
    )

    assert_report_consistent(report)
    assert _tool_status(report) == STATUS_SENT, report.summary()
    assert report.source_sends == (({"type": "forward", "data": {"id": DEFAULT_RESOURCE_ID}},),), report.summary()
    assert report.comment_sends == ("未展开卡片评论",), report.summary()
    assert fixture.clock.sleeps == [0.5], report.summary()
    assert "未展开内部正文" not in rendered(report), "内部节点材料不得出现在发送或结果里"
    fixture.close()


def test_评论阶段与源阶段严格先后且评论只来自本次参数(tmp_path: Path) -> None:
    """顺序证据：平台调用记录里源发送先于评论发送，且两段之间恰有一次 0.5 秒等待。"""

    fixture = make_client_service(tmp_path, raw_message=[text_segment("源")])
    comment = "只来自本次参数"

    result, _ = run_bound_forward(
        fixture.plugin,
        message_id="m-source-1",
        comment=comment,
        extra_arguments={"comment_delay": 0, "delay": 0},
        clock=fixture.clock,
    )
    stages = [call.detail.get("stage") for call in fixture.onebot.send_calls]
    report = scenario_report(
        fixture,
        label="顺序与间隔",
        tool=result,
        input=[text_segment("源")],
        record=fixture.record(),
    )

    assert_report_consistent(report)
    assert stages == [STAGE_PLATFORM_COMMIT, STAGE_COMMENT], ("源发送必须先于评论发送", stages)
    assert fixture.clock.sleeps == [0.5], "两段之间恰有一次固定 0.5 秒等待"
    assert report.comment_sends == (comment,), report.summary()
    assert str(result.get("comment_status")) == STATUS_SENT, report.summary()
    fixture.close()


def test_目标群白名单只影响路由不影响材料且发送链零宿主旧通道(tmp_path: Path) -> None:
    """回归 7.1 的边界：发送目标只来自工具参数与白名单，源正文原样发送。"""

    fixture = make_client_service(tmp_path, raw_message=[text_segment("正文 A")])

    result, arguments = run_bound_forward(
        fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID
    )
    report = scenario_report(
        fixture,
        label="路由与材料",
        tool=result,
        input=[text_segment("正文 A")],
        record=fixture.record(),
    )

    assert arguments["target_group_id"] == TARGET_GROUP_ID
    assert_report_consistent(report)
    assert all(call.target == TARGET_GROUP_ID for call in fixture.onebot.send_calls), report.summary()
    assert report.source_sends[0] == ({"type": "text", "data": {"text": "正文 A"}},), report.summary()
    assert fixture.old_send_calls() == []
    fixture.close()
