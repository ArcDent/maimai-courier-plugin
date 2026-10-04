"""第 4 组投递编排迁移版（4.2–4.6）：客户端经专用服务执行投递与防重。

迁移口径（本变更第 6 组）：本地 SQLite 账本与发送步骤已退出调用图，「唯一投递真相」
在服务端账本。本文件因此覆盖**客户端编排**对这些服务端语义的使用：

- 同键并发 → 服务端原子占位，最多一次平台发送，第二个调用得到 `sending`；
- 不同目标群是不同投递身份，各自独立发送；
- 占位写入失败 / 账本不可用 → `ledger_unavailable`，零发送、无旧通道回退；
- 平台调用期间不持有数据库写锁（独立连接可读可写）；
- 成功回执立即落账、跨服务重启防重；成功/未知记录不因时间清除；
- 明确失败可显式重试；超时/回执丢失按待核实；回执丢失但只读查询可达时按真实结果恢复；
- 契约外状态与「终态未落盘」的客户端映射；
- 崩溃遗留 `sending` 启动归并为未知并继续阻断；客户端不启动后台自动重试；
- 客户端取消不撤销服务端已开始的平台调用（有界等待后仍按真实结果登记）；
- 本地迁移收据损坏或服务不可达时关闭发送，零旧发送通道调用。

服务端账本自身的完整语义（四字段唯一键、短事务、损坏库处理、兼容导出等）已有
第 3 组用例覆盖；本文件不重复实现它们，只验证客户端如何消费这些公开事实。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from delivery_ledger import STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_UNKNOWN
from service.ledger_store import SERVICE_LEDGER_FILE_NAME, ServiceLedger
from tests.service_support import state_ledger_rows
from shared.delivery import DeliveryKey

from remote_client import SERVICE_BINDING_RECEIPT_FILE_NAME

from tests.client_support import make_client_service, make_scripted_plugin
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


def _key(message_id: str = "m-source-1", target_group_id: str = TARGET_GROUP_ID) -> DeliveryKey:
    return DeliveryKey(
        account_id=SOURCE_ACCOUNT_ID,
        source_group_id=SOURCE_GROUP_ID,
        source_message_id=message_id,
        target_group_id=target_group_id,
    )


def _leftover_sending(ledger: ServiceLedger, *, message_id: str = "m-source-1") -> None:
    """用服务端账本的公开方法制造一条崩溃遗留的 `sending` 占位（不经过平台）。"""

    key = _key(message_id)
    attempt_id = f"a-leftover-{message_id}"
    assert asyncio.run(
        ledger.prepare_attempt(
            key,
            attempt_id=attempt_id,
            request_id="req-leftover",
            session_id="s-leftover",
            policy_generation=1,
        )
    ) is not None
    reservation = asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id="req-leftover",
            session_id="s-leftover",
            policy_generation=1,
        )
    )
    assert reservation.granted is True, reservation


# --------------------------------------------------------------------------- 4.2


def test_同键并发调用最多一次进入源发送(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
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
    fixture = make_client_service(
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
    fixture = make_client_service(tmp_path)
    fixture.service.gated_ledger.fail_next("reserve_source")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == "ledger_unavailable", result
    assert "账本" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0, "发送前占位失败不得进入平台调用"
    assert fixture.old_send_calls() == [], "不得回退宿主旧发送通道"
    fixture.close()


def test_网络等待期间不持有数据库事务(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    fixture.onebot.gates.pause("platform_commit")

    async def scenario() -> list[tuple[str, str, str]]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("platform_commit", 0) == 1)
        in_flight = state_ledger_rows(fixture.service.state_dir, message_id="m-source-1")
        # 独立连接必须能在「网络等待」期间取得写事务：说明服务端没有跨网络持有事务。
        connection = sqlite3.connect(
            str(fixture.service.state_dir / SERVICE_LEDGER_FILE_NAME), timeout=1.0, isolation_level=None
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
    fixture = make_client_service(tmp_path)

    first, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert first["success"] is True, first
    assert first["status"] == STATUS_SENT, first
    assert first["sent"] is True and first["delivered"] is True, first
    target_message_id = first["target_message_id"]
    assert target_message_id, first
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT
    assert record["target_message_id"] == target_message_id, "真实回执必须立即保存到权威账本"

    # 模拟服务进程重启：账本事实保留，旧会话失效。
    fixture.restart_service()
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
    """服务端账本不提供删除/TTL 接口；成功记录在时间推进与重启后仍然阻断。"""

    public_methods = [name for name in dir(ServiceLedger) if not name.startswith("_")]
    assert not any(
        keyword in name.lower()
        for name in public_methods
        for keyword in ("delete", "purge", "cleanup", "ttl")
    ), f"账本不得提供删除或 TTL 接口: {public_methods}"

    fixture = make_client_service(tmp_path)
    first, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    target_message_id = first["target_message_id"]
    assert target_message_id

    # 推进十年虚拟时间并重启服务：成功记录仍在，且继续阻断重发。
    fixture.clock.advance(10 * 365 * 24 * 3600)
    fixture.restart_service()

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
    plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path)
    transport.source_reply = {"status": STATUS_FAILED, "reason": "平台明确未发送"}

    first, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID, clock=clock)

    assert first["success"] is False, first
    assert first["status"] == STATUS_FAILED, first
    assert first["sent"] is False and first["delivered"] is False, first
    assert "本次未发送任何消息" in str(first["reason"]), first
    assert transport.count("/source") == 1

    # 明确未发送：允许后续显式调用重试（清掉脚本后同一键真正发送）。
    transport.source_reply = None
    second, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID, clock=clock)

    assert second["success"] is True, second
    assert second["target_message_id"], second
    assert transport.count("/source") == 2, "重试必须真正再次提交源阶段"


def test_超时或丢失回执返回待核实且阻止重发(tmp_path: Path) -> None:
    # 1) 平台回执不可核实：结果未知，重复调用仍按待核实阻断。
    fixture = make_client_service(tmp_path / "unknown")
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

    # 2) 执行响应丢失且查询也不可达：未知 + 阻断，绝不再次提交发送探测。
    lost = make_client_service(tmp_path / "lost")
    lost.transport.drop_response_paths.append("/source")
    lost.transport.fail_paths.append(f"/v1/forwards/{lost.delivery_id()}")

    result, _ = run_bound_forward(lost.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["status"] == STATUS_UNKNOWN, result
    assert "查询不可达" in str(result["reason"]) or "待核实" in str(result["reason"]), result
    assert lost.onebot.send_count == 1, "响应丢失不得触发第二次发送"
    assert lost.transport.count("/source") == 1, "不得再次提交发送探测"
    lost.close()


def test_执行响应丢失但查询可达时按真实结果恢复(tmp_path: Path) -> None:
    """D8：响应丢失后只查询同一投递；已持久化的成功结果按 sent 报告，不重复发送。"""

    fixture = make_client_service(tmp_path)
    fixture.transport.drop_response_paths.append("/source")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")

    assert result["status"] == STATUS_SENT, result
    assert result["sent"] is True and result["target_message_id"], result
    assert fixture.onebot.send_count == 1, "恢复只读查询不得再次发送"
    assert fixture.transport.count("/source") == 1, "不得再次提交发送探测"
    assert fixture.transport.count(f"/v1/forwards/{fixture.delivery_id()}") == 1, (
        "恢复必须恰好查询同一投递一次"
    )
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT
    fixture.close()


def test_未识别发送状态一律按待核实处理且阻止评论(tmp_path: Path) -> None:
    """契约外的状态字面量（空串、大小写不符、超时/取消等）不得降级为可重试失败。"""

    for index, status in enumerate(("", "seNd", "cancelled", "timeout")):
        plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path / f"unrecognized-{index}")
        transport.source_reply = {"status": status, "reason": f"发送步骤返回 {status!r}"}

        result, _ = run_bound_forward(
            plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID, comment="不该发出的评论", clock=clock
        )

        assert result["success"] is False, (status, result)
        assert result["status"] == STATUS_UNKNOWN, (status, "未识别状态必须按待核实兜底", result)
        assert result["sent"] is False and result["delivered"] is False, (
            status,
            "未识别状态不得声称发送成功",
            result,
        )
        assert "待核实" in str(result["reason"]), (status, result)
        assert transport.count("/comment") == 0, (status, "源未确认成功不得进入评论阶段")


def test_终态登记失败时不得宣称可重试失败(tmp_path: Path) -> None:
    """平台已成功但源终态未落盘：报告已知回执但按待核实、阻止重发。"""

    fixture = make_client_service(tmp_path)
    fixture.service.gated_ledger.fail_next("finalize_source")

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
    cases: list[tuple[Any, ...]] = []
    for index, status in enumerate((STATUS_FAILED, STATUS_UNKNOWN)):
        plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path / f"case-{index}")
        transport.source_reply = {"status": status, "reason": f"{status} 的发送结果"}
        cases.append((plugin, transport, clock, issue_source_call(plugin, message_id="m-source-1")))

    async def scenario() -> list[int]:
        extra_sends: list[int] = []
        for plugin, transport, clock, arguments in cases:
            await plugin.forward_message(**arguments)
            baseline = transport.count("/source")
            clock.advance(6 * 3600)
            for _ in range(20):
                await asyncio.sleep(0)
            lingering = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]
            assert lingering == [], f"不得留下后台重试任务: {lingering}"
            extra_sends.append(transport.count("/source") - baseline)
        return extra_sends

    assert asyncio.run(scenario()) == [0, 0], "明确失败与结果未知都不得启动后台自动重试"


# --------------------------------------------------------------------------- 4.5


def test_遗留sending重启后进入待核实且不盲目重发(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    _leftover_sending(fixture.service.ledger)
    assert state_ledger_rows(fixture.service.state_dir, message_id="m-source-1") == [
        ("m-source-1", STATUS_SENDING, "")
    ]

    # 服务重启，并执行启动恢复（CLI `service start` 的同一步骤）：遗留 sending 归并为未知。
    fixture.restart_service()
    asyncio.run(fixture.service.ledger.recover_interrupted())
    recovered = state_ledger_rows(fixture.service.state_dir, message_id="m-source-1")
    assert recovered == [("m-source-1", STATUS_UNKNOWN, "")], recovered

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == STATUS_UNKNOWN, result
    assert "待核实" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0, "遗留占位不得被盲目重发"
    assert fixture.old_send_calls() == []
    fixture.close()


def test_客户端取消不撤销服务端已开始的平台调用(tmp_path: Path) -> None:
    """客户端在平台调用在途时退出：服务端仍保留真实结果，客户端不误判为失败。"""

    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    fixture.onebot.gates.pause("platform_commit")

    async def scenario() -> None:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("platform_commit", 0) == 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - 取消必须在等待点生效
            raise AssertionError("在途投递任务应被取消")
        # 服务端已开始的平台调用在 shield 保护下继续，回执与落账照常完成。
        fixture.onebot.gates.release("platform_commit")
        await wait_until(
            lambda: [row[1] for row in state_ledger_rows(fixture.service.state_dir, message_id="m-source-1")]
            == [STATUS_SENT]
        )

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, (
        "客户端取消不得把服务端已完成的平台调用误判为未发送",
        record,
    )
    assert record["target_message_id"], record
    sends_before = fixture.onebot.send_count
    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    assert repeat["status"] == "already_forwarded", repeat
    assert fixture.onebot.send_count == sends_before, "客户端取消遗留的投递不得盲目重发"
    fixture.close()


def test_发送成功后落盘失败不发送评论并保留已知回执(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    fixture.service.gated_ledger.fail_next("finalize_source")

    result, _ = run_bound_forward(
        fixture.plugin, message_id="m-source-1", comment="不该发出的评论", clock=fixture.clock
    )

    assert result["success"] is False, result
    assert result["status"] == STATUS_UNKNOWN, result
    assert result["sent"] is True and result["delivered"] is True, result
    assert result["target_message_id"], "已知回执必须保留用于报告"
    assert "未发送评论" in str(result["reason"]), result
    assert fixture.onebot.send_count == 1, "落盘失败后不得继续发送评论"
    assert fixture.transport.count("/comment") == 0, "落盘失败后不得提交评论授权"
    assert fixture.clock.sleeps == [], "源终态未固定时不得开始评论等待"
    fixture.close()


def test_落盘失败与占位未命中的措辞可区分(tmp_path: Path) -> None:
    """两类「状态未登记」都按待核实报告，但原因必须区分存储失败与占位未命中。"""

    cases = (
        ("存储失败", "源发送结果未能落盘（注入的账本写入失败）", "未能落盘"),
        ("占位未命中", "源发送结果未能命中在途占位，按待核实处理", "未能命中在途占位"),
    )
    for index, (label, reason, expected) in enumerate(cases):
        plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path / f"persist-{index}")
        transport.source_reply = {
            "status": STATUS_SENT,
            "target_message_id": f"m-target-{index}",
            "reason": reason,
        }

        result, _ = run_bound_forward(
            plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID, comment="不发送", clock=clock
        )

        assert result["status"] == STATUS_UNKNOWN, (label, result)
        assert result["sent"] is True and result["target_message_id"] == f"m-target-{index}", (label, result)
        rendered = str(result["reason"])
        assert "待核实" in rendered and "未发送评论" in rendered, (label, result)
        assert expected in rendered, (label, f"必须区分失败原因（{expected}）", result)
        assert transport.count("/comment") == 0, (label, "源终态未固定不得进入评论")


# --------------------------------------------------------------------------- 4.6


def test_账本不可用时关闭发送且不覆盖状态数据(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    ledger_path = fixture.service.state_dir / SERVICE_LEDGER_FILE_NAME
    # 服务端在写入源占位时发现账本不可用：在进入平台调用前关闭发送。
    fixture.service.gated_ledger.fail_next("reserve_source")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == "ledger_unavailable", result
    reason = str(result["reason"])
    assert "账本" in reason, ("必须输出明确原因", result)
    assert "本次未发送任何消息" in reason, result
    assert fixture.onebot.send_count == 0, "账本不可用时不得发送"
    assert fixture.old_send_calls() == [], "不得回退旧发送通道"

    # 账本仍不可用：重复调用同样关闭发送，且状态数据保持原样。
    fixture.service.gated_ledger.fail_next("reserve_source")
    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert repeat["success"] is False and repeat["status"] == "ledger_unavailable", repeat
    assert ledger_path.exists() and ledger_path.read_bytes().startswith(b"SQLite format 3"), (
        "不得删除或覆盖状态数据（文件仍是可读的 SQLite 库）"
    )
    assert state_ledger_rows(fixture.service.state_dir) == [], (
        "账本不可用期间不得留下任何投递记录",
    )
    fixture.close()


def test_本地迁移收据损坏时关闭发送(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    receipt = fixture.data_dir / SERVICE_BINDING_RECEIPT_FILE_NAME
    receipt.write_text("{not json", encoding="utf-8")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")

    assert result["success"] is False, result
    assert result["status"] == "rejected", result
    assert "收据" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0
    assert fixture.old_send_calls() == []
    fixture.close()


def test_服务不可达时关闭发送(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)
    fixture.transport.fail_paths.append("/v1/sessions")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == "rejected", result
    assert "转发服务" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0, "服务不可达时不得发送"
    assert fixture.old_send_calls() == []
    fixture.close()


def test_会话失联后重复调用查询一次并促成评论归并(tmp_path: Path) -> None:
    """第 5 组交接口径：会话失联后由客户端查询一次，促成服务端登记跳过待提交评论。

    客户端不假设服务端会自行补发：重复工具调用对同键返回既有源结果，客户端只读
    查询同一投递让服务端把不再可提交的评论登记为 `skipped`，再按权威状态报告。
    """

    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间失联")

    async def scenario() -> None:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.clock.sleeps == [0.5])
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT
    assert record["comment_status"] == "pending", ("未提交评论应保持待提交", record)
    assert fixture.transport.count("/comment") == 0, "取消后不得提交评论授权"

    # 挑战与旧会话都过期后，新实例重复调用：查询一次，服务端把待提交评论登记为跳过。
    fixture.clock.advance(31.0)
    again = fixture.fresh_plugin()
    repeat, _ = run_bound_forward(
        again.plugin, message_id="m-source-1", comment="重复调用不补发", clock=again.clock
    )

    assert repeat["status"] == "already_forwarded", repeat
    assert repeat["comment_status"] == STATUS_FAILED, (
        "评论未提交且授权已失效：工具结果面按「明确未发送」报告",
        repeat,
    )
    assert "跳过" in str(repeat["reason"]), repeat
    assert fixture.onebot.send_count == 1, "重复调用不得重发源消息或补发评论"
    assert fixture.transport.count("/comment") == 0, "不得为评论生成新授权"

    settled = fixture.record()
    assert settled is not None and settled["comment_status"] == "skipped", (
        "只读查询必须促成服务端把待提交评论登记为跳过",
        settled,
    )
    fixture.close()
