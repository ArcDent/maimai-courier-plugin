"""公开工具到直连与本地账本：并发、占位、权限竞态、落账失败与重启阻断。

保留原投递用例的产品断言；已退出的远端会话协议改由公开固定平台动作及本地账本观察。
新增真实传输排队、容量与不合作退出用例，不使用私有复验作为验收入口。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from delivery_ledger import STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_UNKNOWN
from delivery_ledger import LEDGER_FILE_NAME, DeliveryLedger
from shared.delivery import DeliveryKey
from tests.direct_support import make_direct_plugin

from tests.support import (
    SOURCE_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    TARGET_GROUP_ID,
    issue_source_call,
    run_bound_forward,
    wait_until,
)

OTHER_TARGET_GROUP_ID = "20003"
"""另一个白名单目标群（不同投递身份，独立发送）。"""


def test_直连工具持久占位先于提交且并发同键只发一次(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin
    fixture = make_direct_plugin(tmp_path)
    first = issue_source_call(fixture.plugin)
    second = issue_source_call(fixture.plugin)
    fixture.onebot.gates.pause("source_queue")

    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**first))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("source_queue") == 1)
        assert fixture.record()["status"] == "sending"
        assert fixture.onebot.send_count == 0
        duplicate = await fixture.plugin.forward_message(**second)
        assert duplicate["status"] == "sending"
        fixture.onebot.gates.release("source_queue")
        result = await task
        assert result["status"] == "sent"
        assert fixture.onebot.send_count == 1
        assert fixture.record()["target_message_id"] == "target-1"
    asyncio.run(scenario())
    fixture.close()


def test_源排队后端点变化阻止旧提交并明确拒绝(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin, config_with
    from tests.support import apply_config_update
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin)
    fixture.onebot.gates.pause("source_queue")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("source_queue") == 1)
        await apply_config_update(fixture.plugin, config_with(fixture.plugin,
            snowluma={"websocket_url": "ws://127.0.0.1:8766/onebot"}))
        fixture.onebot.gates.release("source_queue")
        result = await task
        assert result["status"] == "rejected"
        assert fixture.onebot.send_count == 0
        assert fixture.record()["status"] == "failed"
    asyncio.run(scenario())
    fixture.close()


def test_评论半秒前零提交且提交前占位可读(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin, comment="这是一条评论")
    fixture.onebot.gates.pause("comment_queue")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: fixture.record() is not None and fixture.record()["status"] == "sent")
        for _ in range(30):
            await asyncio.sleep(0)
        assert not task.done()
        assert fixture.onebot.send_count == 1
        assert fixture.record()["comment_status"] == ""
        fixture.clock.advance(0.49)
        for _ in range(30):
            await asyncio.sleep(0)
        assert fixture.onebot.send_count == 1
        fixture.clock.advance(0.01)
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("comment_queue") == 1)
        assert fixture.record()["comment_status"] == "sending"
        fixture.onebot.gates.release("comment_queue")
        result = await task
        assert result["status"] == "sent" and result["comment_status"] == "sent"
        assert fixture.onebot.send_count == 2
        assert fixture.record()["comment_message_id"] == "target-2"
        assert fixture.ctx.maisaka.proactive.calls == []
    asyncio.run(scenario())
    fixture.close()


def test_端点热更新只允许新调用使用新连接且旧评论不补发(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin, config_with, DirectOneBot
    from tests.support import apply_config_update, prepare_source
    fixture = make_direct_plugin(tmp_path)
    old = fixture.onebot
    args = issue_source_call(fixture.plugin, comment="旧连接评论")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: 0.5 in fixture.clock.sleeps)
        await apply_config_update(fixture.plugin, config_with(fixture.plugin,
            snowluma={"websocket_url": "ws://127.0.0.1:8766/onebot"}))
        fixture.clock.advance(0.5)
        result = await task
        assert result["status"] == "partial" and result["target_message_id"] == "target-1"
        assert old.send_count == 1
    asyncio.run(scenario())
    fixture.onebot = DirectOneBot(fixture.ctx, (TARGET_GROUP_ID,))
    prepare_source(fixture.ctx, message_id="m-new")
    result, _ = run_bound_forward(fixture.plugin, message_id="m-new")
    assert result["status"] == "sent"
    assert old.closed and fixture.onebot.send_count == 1
    fixture.close()


def test_源与评论终态落盘失败保留真实ID且重开仍阻断(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin
    from tests.support import open_test_ledger, ledger_key
    for operation, count in (("mark_sent", 1), ("record_comment", 2)):
        fixture = make_direct_plugin(tmp_path / operation)
        fixture.storage.fail_next(operation)
        args = issue_source_call(fixture.plugin, comment="不能补发")
        async def scenario():
            task = asyncio.create_task(fixture.plugin.forward_message(**args))
            for _ in range(200):
                await asyncio.sleep(0)
                if 0.5 in fixture.clock.sleeps:
                    fixture.clock.advance(0.5)
                if task.done():
                    return await task
            raise AssertionError("工具未结束")
        result = asyncio.run(scenario())
        assert result["success"] is False and result["sent"] is True
        assert result["target_message_id"] == "target-1"
        assert result["stage"] == ("source_persist" if count == 1 else "comment_persist")
        assert fixture.onebot.send_count == count
        if count == 2:
            assert result["comment_status"] == "unknown"
            assert result["comment_message_id"] == "target-2"
        fixture.close()
        reopened = open_test_ledger(tmp_path / operation)
        record = reopened.get(ledger_key())
        assert record.status == ("unknown" if count == 1 else "sent")
        assert reopened.reserve(ledger_key()).granted is False
        if count == 2:
            assert record.comment_status == "unknown"
        reopened.close()


def test_八个在途投递时第九个繁忙且退出后释放容量(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin
    from tests.support import source_message, SOURCE_SESSION_ID
    fixture = make_direct_plugin(tmp_path)
    for index in range(10):
        fixture.ctx.message.store[(SOURCE_SESSION_ID, str(index))] = source_message(message_id=str(index))
    arguments = [issue_source_call(fixture.plugin, message_id=str(index)) for index in range(10)]
    fixture.onebot.gates.pause("get_login_info")
    async def scenario():
        tasks = [asyncio.create_task(fixture.plugin.forward_message(**args)) for args in arguments[:8]]
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("get_login_info") == 1)
        for _ in range(40):
            await asyncio.sleep(0)
        ninth = asyncio.create_task(fixture.plugin.forward_message(**arguments[8]))
        for _ in range(40):
            await asyncio.sleep(0)
        assert ninth.done(), "第九个应立即繁忙，不能进入等待队列"
        assert (await ninth)["status"] == "rejected"
        assert "繁忙" in (await ninth)["reason"]
        assert len(fixture.ctx.message.calls) == 8
        fixture.onebot.gates.release("get_login_info")
        assert all(result["status"] == "sent" for result in await asyncio.gather(*tasks))
        result = await fixture.plugin.forward_message(**arguments[9])
        assert result["status"] == "sent"
        assert fixture.onebot.send_count == 9
    asyncio.run(scenario())
    fixture.close()


def test_卸载共享五秒预算且残留工作保持账本独占(tmp_path: Path) -> None:
    from tests.direct_support import make_direct_plugin
    from tests.support import ledger_key
    from delivery_ledger import open_ledger, LEDGER_FILE_NAME, LedgerUnavailable
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin)
    entered, release = asyncio.Event(), asyncio.Event()
    async def stubborn(stage):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
    fixture.onebot.before_wire = stubborn
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await entered.wait()
        unloading = asyncio.create_task(fixture.plugin.on_unload())
        for _ in range(40):
            await asyncio.sleep(0)
        fixture.clock.advance(5)
        try:
            await wait_until(unloading.done)
            await unloading
        finally:
            release.set()
        assert fixture.onebot.send_count == 0
        assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable)
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        assert "未退出" in warnings and "m-source-1" in warnings and "20002" in warnings
        assert fixture.ledger.get(ledger_key()).status == "failed"
        release.set()
        await task
        for _ in range(30):
            await asyncio.sleep(0)
        assert fixture.onebot.send_count == 0
    asyncio.run(scenario())
    fixture.ledger.close()


def test_真实传输排队结束后复验最新权限且零发送帧(tmp_path: Path) -> None:
    import pytest
    pytest.importorskip("aiohttp")
    import json
    from types import SimpleNamespace
    from aiohttp import WSMsgType
    from maimai_courier_plugin.snowluma_transport import AiohttpOneBotTransport
    from tests.snowluma_transport_support import MemorySocket, MemorySession
    from tests.direct_support import config_with
    from tests.support import make_plugin, prepare_source, enabled_config, apply_config_update, open_test_ledger, ledger_key
    from tests.support import FakeClock
    clock = FakeClock()
    ledger = open_test_ledger(tmp_path, clock=clock)
    socket = MemorySocket()
    session = MemorySession(socket)
    connections = []
    config = enabled_config()
    config["snowluma"] = {"websocket_url": "ws://127.0.0.1:8765/onebot", "token_env_var": "SYNTHETIC_TOKEN"}
    def factory(connection):
        transport = AiohttpOneBotTransport(connection, clock=clock, environ={"SYNTHETIC_TOKEN": "synthetic"}, session_factory=lambda **_: session)
        connections.append(transport)
        return transport
    plugin, ctx = make_plugin(tmp_path, config=config, clock=clock, transport_factory=factory,
                              environ={"SYNTHETIC_TOKEN": "synthetic"}, ledger_opener=lambda _: ledger)
    prepare_source(ctx)
    args = issue_source_call(plugin)
    blocked, blocker = [], []
    logins = 0
    def reply(body, data):
        socket.frames.put_nowait(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
            "status": "ok", "retcode": 0, "echo": body["echo"], "data": data})))
    async def send(body):
        nonlocal logins
        socket.sent.append(body)
        action = body["action"]
        if action == "get_login_info":
            logins += 1
            if logins == 2:
                blocker.append(asyncio.create_task(connections[0].get_msg("queue-blocker")))
            reply(body, {"user_id": "10000"})
        elif action == "get_group_list":
            reply(body, [{"group_id": "20002"}])
        elif action == "get_msg" and body["params"]["message_id"] == "queue-blocker":
            blocked.append(body)
        elif action == "get_msg":
            reply(body, {"message_id": "m-source-1", "message_type": "group", "group_id": "10001",
                         "message": [{"type": "text", "data": {"text": "原文"}}]})
    socket.send_json = send
    async def scenario():
        task = asyncio.create_task(plugin.forward_message(**args))
        await wait_until(lambda: bool(blocked))
        await apply_config_update(plugin, config_with(plugin, scope={"target_groups": []}))
        reply(blocked[0], {})
        await blocker[0]
        result = await task
        assert result["status"] == "rejected"
        assert [frame for frame in socket.sent if frame["action"] == "send_msg"] == []
        assert ledger.get(ledger_key()).status == "failed"
        await plugin.on_unload()
    asyncio.run(scenario())


def _key(message_id: str = "m-source-1", target_group_id: str = TARGET_GROUP_ID) -> DeliveryKey:
    return DeliveryKey(
        account_id=SOURCE_ACCOUNT_ID,
        source_group_id=SOURCE_GROUP_ID,
        source_message_id=message_id,
        target_group_id=target_group_id,
    )


def _leftover_sending(ledger: DeliveryLedger, *, message_id: str = "m-source-1") -> None:
    assert ledger.reserve(_key(message_id)).granted


# --------------------------------------------------------------------------- 4.2


def test_同键并发调用最多一次进入源发送(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    first_arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    second_arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    fixture.onebot.gates.pause("platform_commit")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        in_flight = asyncio.create_task(fixture.plugin.forward_message(**first_arguments))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("platform_commit", 0) == 1)
        concurrent = await fixture.plugin.forward_message(**second_arguments)
        fixture.onebot.gates.release("platform_commit")
        return await asyncio.wait_for(in_flight, timeout=5), concurrent

    first, concurrent = asyncio.run(scenario())

    assert fixture.onebot.send_count == 1, "同键并发最多只能有一次进入源发送"
    assert first["success"] is True and first["status"] == STATUS_SENT, first
    assert first["target_message_id"], first
    assert concurrent["success"] is False, concurrent
    assert concurrent["status"] == STATUS_SENDING, concurrent
    assert "发送中" in str(concurrent["reason"]), concurrent

    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, record
    fixture.close()


def test_不同目标群独立发送(tmp_path: Path) -> None:
    fixture = make_direct_plugin(
        tmp_path,
        target_groups=(TARGET_GROUP_ID, OTHER_TARGET_GROUP_ID),
    )

    first, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    second, _ = run_bound_forward(
        fixture.plugin, message_id="m-source-1", target_group_id=OTHER_TARGET_GROUP_ID
    )

    assert first["success"] is True and second["success"] is True, (first, second)
    assert fixture.onebot.send_count == 2, "不同目标群是不同键，必须各自发送"
    assert [call.target for call in fixture.onebot.send_calls] == [TARGET_GROUP_ID, OTHER_TARGET_GROUP_ID]
    assert fixture.record(target_group_id=TARGET_GROUP_ID)["status"] == STATUS_SENT
    assert fixture.record(target_group_id=OTHER_TARGET_GROUP_ID)["status"] == STATUS_SENT
    fixture.close()


def test_发送前持久化失败时发送次数为零(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    fixture.storage.fail_next("reserve")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == "ledger_unavailable", result
    assert "账本" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0, "发送前占位失败不得进入平台调用"
    assert fixture.old_send_calls() == [], "不得回退宿主旧发送通道"
    fixture.close()


def test_网络等待期间不持有数据库事务(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    fixture.onebot.gates.pause("platform_commit")

    async def scenario() -> list[tuple[str, str, str]]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("platform_commit", 0) == 1)
        record = fixture.ledger.get(_key())
        in_flight = [(record.key.source_message_id, record.status, record.target_message_id)]
        # 独立连接必须能在「网络等待」期间取得写事务：说明插件没有跨网络持有事务。
        connection = sqlite3.connect(
            str(fixture.data_dir / LEDGER_FILE_NAME), timeout=1.0, isolation_level=None
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.close()
        fixture.onebot.gates.release("platform_commit")
        await asyncio.wait_for(task, timeout=5)
        return in_flight

    in_flight = asyncio.run(scenario())

    assert in_flight == [("m-source-1", STATUS_SENDING, "")], (
        "源占位必须在进入平台调用前提交（独立连接可见）",
        in_flight,
    )
    assert fixture.record()["status"] == STATUS_SENT
    fixture.close()


# --------------------------------------------------------------------------- 4.3


def test_成功回执立即落账并跨重启防重(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)

    first, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert first["success"] is True, first
    assert first["status"] == STATUS_SENT, first
    assert first["sent"] is True and first["delivered"] is True, first
    target_message_id = first["target_message_id"]
    assert target_message_id, first
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT
    assert record["target_message_id"] == target_message_id, "真实回执必须立即保存到权威账本"

    # 关闭旧插件并重新打开同一本地账本，投递事实继续阻断。
    fixture.reopen()
    sends_before = fixture.onebot.send_count

    repeat, _ = run_bound_forward(
        fixture.plugin,
        message_id="m-source-1",
        target_group_id=TARGET_GROUP_ID,
        comment="换一条评论也不得绕过防重",
    )

    assert repeat["success"] is True, repeat
    assert repeat["status"] == "already_forwarded", repeat
    assert repeat["already_forwarded"] is True, repeat
    assert repeat["target_message_id"] == target_message_id, "已转发结果必须带回既有目标消息 ID"
    assert "未发送源消息或评论" in str(repeat["reason"]), repeat
    assert fixture.onebot.send_count == sends_before, "跨重启的既有记录必须阻止源消息与评论重发"
    assert fixture.old_send_calls() == []
    fixture.close()


def test_成功记录不因时间或容量被静默清除(tmp_path: Path) -> None:
    """插件账本不提供删除/TTL 接口；成功记录在时间推进与重启后仍然阻断。"""

    public_methods = [name for name in dir(DeliveryLedger) if not name.startswith("_")]
    assert not any(
        keyword in name.lower()
        for name in public_methods
        for keyword in ("delete", "purge", "cleanup", "ttl")
    ), f"账本不得提供删除或 TTL 接口: {public_methods}"

    fixture = make_direct_plugin(tmp_path)
    first, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    target_message_id = first["target_message_id"]
    assert target_message_id

    # 推进十年虚拟时间并重载插件：成功记录仍在，且继续阻断重发。
    fixture.clock.advance(10 * 365 * 24 * 3600)
    fixture.reopen()

    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, (
        "成功记录不得按时间静默清除",
        record,
    )
    assert record["target_message_id"] == target_message_id
    sends_before = fixture.onebot.send_count
    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    assert repeat["status"] == "already_forwarded", repeat
    assert fixture.onebot.send_count == sends_before
    fixture.close()


# --------------------------------------------------------------------------- 4.4


def test_明确未发送的失败可重试(tmp_path: Path) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
    fixture = make_direct_plugin(tmp_path)
    async def denied(stage):
        raise OneBotUnavailable("not_submitted", submitted=False)
    fixture.onebot.before_wire = denied
    first, _ = run_bound_forward(fixture.plugin)
    assert first["status"] == "failed" and not first["success"] and not first["sent"]
    assert fixture.record()["status"] == "failed" and fixture.onebot.send_count == 0
    fixture.onebot.before_wire = None
    second, _ = run_bound_forward(fixture.plugin)
    assert second["status"] == "sent" and second["target_message_id"] == "target-1"
    assert fixture.onebot.send_count == 1
    fixture.close()


def test_超时或丢失回执返回待核实且阻止重发(tmp_path: Path) -> None:
    # 1) 平台回执不可核实：结果未知，重复调用仍按待核实阻断。
    fixture = make_direct_plugin(tmp_path / "unknown")
    fixture.onebot.fail_next("send_msg", {"status": "failed", "retcode": 79, "message": "rejected"})

    first, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert first["success"] is False, first
    assert first["status"] == STATUS_UNKNOWN, first
    assert first["sent"] is False, ("结果未知不得伪报成功", first)
    assert "待核实" in str(first["reason"]), first
    assert fixture.onebot.send_count == 1, first

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert repeat["success"] is False and repeat["status"] == STATUS_UNKNOWN, repeat
    assert fixture.onebot.send_count == 1, "待核实记录必须阻止重发"
    fixture.close()

    # 动作响应丢失：即使服务器保存了目标，也不能猜 ID 或再次发送。
    lost = make_direct_plugin(tmp_path / "lost")
    lost.onebot.fail_next("send_msg", TimeoutError("合成回执丢失"))
    result, _ = run_bound_forward(lost.plugin)
    assert result["status"] == "unknown" and lost.onebot.send_count == 1
    repeat, _ = run_bound_forward(lost.plugin)
    assert repeat["status"] == "unknown" and lost.onebot.send_count == 1
    lost.close()


def test_已取得回执只查询同一目标不重复发送(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    result, _ = run_bound_forward(fixture.plugin)
    assert result["status"] == "sent" and fixture.onebot.send_count == 1
    queried = [value for name, value, _ in fixture.onebot.calls if name == "get_msg"]
    assert queried == ["m-source-1", "target-1"]
    assert fixture.record()["target_message_id"] == "target-1"
    fixture.close()


def test_未识别发送状态一律按待核实处理且阻止评论(tmp_path: Path) -> None:
    for index, status in enumerate(("", "seNd", "cancelled", "timeout")):
        fixture = make_direct_plugin(tmp_path / str(index))
        fixture.onebot.fail_next("send_msg", {"status": status, "data": {"message_id": "target-1"}})
        result, _ = run_bound_forward(fixture.plugin, comment="不该发送")
        assert result["status"] == "unknown" and not result["sent"] and not result["success"]
        assert fixture.onebot.send_count == 1 and fixture.record()["status"] == "unknown"
        assert fixture.record()["comment_status"] == ""
        fixture.close()


def test_终态登记失败时不得宣称可重试失败(tmp_path: Path) -> None:
    """平台已成功但源终态未落盘：报告已知回执但按待核实、阻止重发。"""

    fixture = make_direct_plugin(tmp_path)
    fixture.storage.fail_next("mark_sent")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["status"] == STATUS_UNKNOWN, ("登记失败不得宣称可重试", result)
    assert result["sent"] is True, ("已知回执必须保留用于报告", result)
    assert result["target_message_id"], result
    assert "待核实" in str(result["reason"]), result
    assert "本次未发送任何消息" not in str(result["reason"]), result
    assert fixture.onebot.send_count == 1

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert repeat["status"] in (STATUS_SENDING, STATUS_UNKNOWN), repeat
    assert fixture.onebot.send_count == 1, "未落盘的在途占位必须继续阻断重发"
    fixture.close()


def test_失败与待核实都不启动后台自动重试(tmp_path: Path) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
    for index, submitted in enumerate((False, True)):
        fixture = make_direct_plugin(tmp_path / str(index))
        if submitted:
            fixture.onebot.fail_next("send_msg", TimeoutError())
        else:
            async def denied(stage):
                raise OneBotUnavailable("not_submitted", submitted=False)
            fixture.onebot.before_wire = denied
        args = issue_source_call(fixture.plugin)
        async def scenario():
            result = await fixture.plugin.forward_message(**args)
            assert result["status"] == ("unknown" if submitted else "failed")
            before = fixture.onebot.send_count
            fixture.clock.advance(6 * 3600)
            for _ in range(30):
                await asyncio.sleep(0)
            assert fixture.onebot.send_count == before
        asyncio.run(scenario())
        fixture.close()


# --------------------------------------------------------------------------- 4.5


def test_遗留sending重启后进入待核实且不盲目重发(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    _leftover_sending(fixture.ledger)
    assert fixture.record()["status"] == "sending"
    fixture.reopen()
    assert fixture.record()["status"] == "unknown"
    result, _ = run_bound_forward(fixture.plugin)
    assert result["status"] == "unknown" and not result["success"]
    assert fixture.onebot.calls == []
    fixture.close()


def test_提交后取消保留未知且阻断重发(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin, comment="不得发送")
    fixture.onebot.gates.pause("platform_commit")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: fixture.onebot.send_count == 1)
        task.cancel()
        result = await task
        assert result["status"] == "unknown" and not result["success"]
        fixture.onebot.gates.release("platform_commit")
    asyncio.run(scenario())
    assert fixture.record()["status"] == "unknown"
    repeat, _ = run_bound_forward(fixture.plugin)
    assert repeat["status"] == "unknown" and fixture.onebot.send_count == 1
    fixture.close()


def test_发送成功后落盘失败不发送评论并保留已知回执(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    fixture.storage.fail_next("mark_sent")

    result, _ = run_bound_forward(
        fixture.plugin, message_id="m-source-1", comment="不该发出的评论", clock=fixture.clock
    )

    assert result["success"] is False, result
    assert result["status"] == STATUS_UNKNOWN, result
    assert result["sent"] is True and result["delivered"] is True, result
    assert result["target_message_id"], "已知回执必须保留用于报告"
    assert "未发送评论" in str(result["reason"]), result
    assert fixture.onebot.send_count == 1, "落盘失败后不得继续发送评论"
    assert sum(call["stage"] == "comment" for call in fixture.onebot.sent) == 0, "落盘失败后不得提交评论授权"
    assert 0.5 not in fixture.clock.sleeps, "源终态未固定时不得开始评论等待"
    fixture.close()


def test_落盘失败与占位未命中的措辞可区分(tmp_path: Path) -> None:
    for index, miss in enumerate((False, True)):
        fixture = make_direct_plugin(tmp_path / str(index))
        if miss:
            fixture.storage.mark_sent = lambda *args: False
        else:
            fixture.storage.fail_next("mark_sent")
        result, _ = run_bound_forward(fixture.plugin, comment="不发送", clock=fixture.clock)
        assert result["status"] == "unknown" and result["sent"]
        assert result["target_message_id"] == "target-1"
        assert result["stage"] == "source_persist" and "未发送评论" in result["reason"]
        assert ("未能命中在途占位" if miss else "未能落盘") in result["reason"]
        assert fixture.onebot.send_count == 1 and fixture.record()["status"] == "sending"
        fixture.close()


# --------------------------------------------------------------------------- 4.6


def test_账本不可用时关闭发送且不覆盖状态数据(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    ledger_path = fixture.data_dir / LEDGER_FILE_NAME
    # 插件在写入源占位时发现账本不可用：在进入平台调用前关闭发送。
    fixture.storage.fail_next("reserve")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == "ledger_unavailable", result
    reason = str(result["reason"])
    assert "账本" in reason, ("必须输出明确原因", result)
    assert "本次未发送任何消息" in reason, result
    assert fixture.onebot.send_count == 0, "账本不可用时不得发送"
    assert fixture.old_send_calls() == [], "不得回退旧发送通道"

    # 账本仍不可用：重复调用同样关闭发送，且状态数据保持原样。
    fixture.storage.fail_next("reserve")
    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert repeat["success"] is False and repeat["status"] == "ledger_unavailable", repeat
    assert ledger_path.exists() and ledger_path.read_bytes().startswith(b"SQLite format 3"), (
        "不得删除或覆盖状态数据（文件仍是可读的 SQLite 库）"
    )
    assert fixture.record() is None, "占位失败不得留下记录"
    fixture.close()


def test_本地迁移收据损坏时关闭发送(tmp_path: Path) -> None:
    from tests.support import make_plugin, prepare_source, enabled_config
    from tests.direct_support import DirectOneBot
    receipt = tmp_path / "service_binding.json"
    receipt.write_text("{not json", encoding="utf-8")
    config = enabled_config()
    config["snowluma"] = {"websocket_url": "ws://127.0.0.1:8765/onebot", "token_env_var": "SYNTHETIC_TOKEN"}
    plugin, ctx = make_plugin(tmp_path, config=config, environ={"SYNTHETIC_TOKEN": "synthetic"}, transport_factory=lambda _: onebot)
    prepare_source(ctx)
    onebot = DirectOneBot(ctx, (TARGET_GROUP_ID,))
    result, _ = run_bound_forward(plugin)
    assert result["status"] == "ledger_unavailable" and not result["sent"]
    assert onebot.calls == [] and receipt.read_text(encoding="utf-8") == "{not json"
    asyncio.run(plugin.on_unload())


def test_直连不可达时关闭发送(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    fixture.onebot.fail_next("get_login_info", ConnectionError("合成连接失败"))
    result, _ = run_bound_forward(fixture.plugin)
    assert result["status"] == "rejected" and not result["success"]
    assert fixture.onebot.send_count == 0 and fixture.old_send_calls() == []
    fixture.close()


def test_评论等待取消后重载重复调用不补发也不额外查询(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin, comment="不得补发")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: 0.5 in fixture.clock.sleeps)
        task.cancel()
        result = await task
        assert result["status"] == "partial" and result["target_message_id"] == "target-1"
    asyncio.run(scenario())
    assert fixture.onebot.send_count == 1
    fixture.reopen()
    repeat, _ = run_bound_forward(fixture.plugin, comment="换评论也不得补发")
    assert repeat["status"] == "already_forwarded" and repeat["target_message_id"] == "target-1"
    assert fixture.onebot.calls == []
    fixture.close()


def test_评论占位失败零评论且重复调用不补发(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    fixture.storage.fail_next("reserve_comment")
    result, _ = run_bound_forward(fixture.plugin, comment="不得越过占位", clock=fixture.clock)
    assert result["status"] == "partial" and result["stage"] == "comment_persist"
    assert result["target_message_id"] == "target-1" and not result["success"]
    assert fixture.onebot.send_count == 1
    assert fixture.record()["status"] == "sent" and fixture.record()["comment_status"] == ""
    baseline = list(fixture.onebot.calls)
    repeat, _ = run_bound_forward(fixture.plugin, comment="重复不补发")
    assert repeat["status"] == "already_forwarded" and fixture.onebot.calls == baseline
    fixture.close()


def test_拒绝异常与取消均释放投递容量(tmp_path: Path) -> None:
    from tests.direct_support import deny_before_wire
    fixture = make_direct_plugin(tmp_path)
    fixture.onebot.before_wire = deny_before_wire
    for _ in range(9):
        result, _ = run_bound_forward(fixture.plugin)
        assert result["status"] == "failed" and fixture.onebot.send_count == 0
    for _ in range(9):
        result, _ = run_bound_forward(fixture.plugin, target_group_id="99999")
        assert result["status"] == "rejected" and "繁忙" not in result["reason"]
    fixture.onebot.before_wire = None
    fixture.onebot.gates.pause("source_queue")
    args = issue_source_call(fixture.plugin)
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("source_queue", 0) == 10)
        task.cancel()
        result = await task
        assert result["status"] == "failed" and fixture.onebot.send_count == 0
        fixture.onebot.gates.release("source_queue")
    asyncio.run(scenario())
    result, _ = run_bound_forward(fixture.plugin)
    assert result["status"] == "sent" and fixture.onebot.send_count == 1
    fixture.close()
