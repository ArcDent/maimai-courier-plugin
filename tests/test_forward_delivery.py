"""第 4 组投递编排（4.2–4.6）：统一经 `forward_message` 工具入口驱动。

「发送进入次数」用替身发送步骤对替身 `ctx.send` 的调用次数表达（第 4 组不接通真实
发送）；账本状态经持久状态模块的导出接口核对。用例只观察工具返回、宿主能力调用与
账本记录，不直接读 SQL。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from delivery_ledger import (
    LEDGER_FILE_NAME,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryLedger,
    Reservation,
    open_ledger,
)
from forward_delivery import TOOL_STATUS_ALREADY_FORWARDED

from tests.support import (
    TARGET_GROUP_ID,
    FakeClock,
    FailingLedger,
    RecordingSendStep,
    enabled_config,
    issue_source_call,
    ledger_key,
    make_plugin,
    open_test_ledger,
    prepare_source,
    run_bound_forward,
    wait_until,
)

OTHER_TARGET_GROUP_ID = "20003"


# --------------------------------------------------------------------------- 4.2


def test_同键并发调用最多一次进入源发送(tmp_path: Path) -> None:
    step = RecordingSendStep()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
    prepare_source(ctx)
    first_arguments = issue_source_call(plugin, message_id="m-source-1")
    second_arguments = issue_source_call(plugin, message_id="m-source-1")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        ctx.send.hold = asyncio.Event()
        in_flight = asyncio.create_task(plugin.forward_message(**first_arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)
        concurrent = await plugin.forward_message(**second_arguments)
        ctx.send.hold.set()
        return await in_flight, concurrent

    first, concurrent = asyncio.run(scenario())

    assert len(ctx.send.calls) == 1, "同键并发最多只能有一次进入源发送"
    assert len(step.entries) == 1
    assert first["success"] is True, first
    assert first["status"] == STATUS_SENT, first
    assert first["target_message_id"] == "m-delivered-1", first
    assert concurrent["success"] is False, concurrent
    assert concurrent["status"] == STATUS_SENDING, concurrent
    assert "发送中" in str(concurrent["reason"]), concurrent

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-delivered-1")


def test_不同目标群独立发送(tmp_path: Path) -> None:
    step = RecordingSendStep()
    plugin, ctx = make_plugin(
        tmp_path,
        config=enabled_config(target_groups=(TARGET_GROUP_ID, OTHER_TARGET_GROUP_ID)),
        send_source=step,
    )
    prepare_source(ctx)

    first, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    second, _ = run_bound_forward(
        plugin,
        message_id="m-source-1",
        target_group_id=OTHER_TARGET_GROUP_ID,
    )

    assert first["success"] is True and second["success"] is True, (first, second)
    assert len(ctx.send.calls) == 2, "不同目标群是不同键，必须各自发送"
    ledger = open_test_ledger(tmp_path)
    assert ledger.get(ledger_key()).status == STATUS_SENT
    assert ledger.get(ledger_key(target_group_id=OTHER_TARGET_GROUP_ID)).status == STATUS_SENT


def test_发送前持久化失败时发送次数为零(tmp_path: Path) -> None:
    step = RecordingSendStep()
    plugin, ctx = make_plugin(
        tmp_path / "blocked",
        config=enabled_config(),
        send_source=step,
        storage_fault=True,
    )
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert "账本" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == [], "发送前持久化失败不得发送"
    assert step.entries == []


def test_网络等待期间不持有数据库事务(tmp_path: Path) -> None:
    observations: list[Any] = []

    def probe() -> None:
        """发送步骤（网络等待）期间用第二个连接观察账本。"""

        second = open_test_ledger(tmp_path)
        observations.append(second.get(ledger_key()))
        observations.append(second.reserve(ledger_key(message_id="m-probe")))
        second.close()

    step = RecordingSendStep(on_enter=probe)
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is True, result
    in_flight, reservation = observations
    assert in_flight is not None and in_flight.status == STATUS_SENDING, (
        "发送前占位必须在进入发送步骤前提交（另一连接可见）"
    )
    assert isinstance(reservation, Reservation) and reservation.granted is True, (
        "网络等待期间不得持有数据库事务（另一连接必须能写入）"
    )
    probe_record = open_test_ledger(tmp_path).get(ledger_key(message_id="m-probe"))
    assert probe_record is not None and probe_record.status == STATUS_SENDING, (
        "另一连接在网络等待期间写入的记录必须已提交"
    )


# --------------------------------------------------------------------------- 4.3


def test_成功回执立即落账并跨重启防重(tmp_path: Path) -> None:
    first_step = RecordingSendStep(target_message_id="m-delivered-42")
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=first_step)
    prepare_source(ctx)

    first, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert first["success"] is True, first
    assert first["status"] == STATUS_SENT, first
    assert first["sent"] is True and first["delivered"] is True, first
    assert first["target_message_id"] == "m-delivered-42", first
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_SENT
    assert record.target_message_id == "m-delivered-42", "真实回执必须立即保存到账本"

    # 模拟重启：新插件实例与新上下文，同一 data_dir（账本重开）
    restart_step = RecordingSendStep()
    restarted, restarted_ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        send_source=restart_step,
    )
    prepare_source(restarted_ctx)
    repeat, _ = run_bound_forward(
        restarted,
        message_id="m-source-1",
        target_group_id=TARGET_GROUP_ID,
        comment="换一条评论也不得绕过防重",
    )

    assert repeat["success"] is True, repeat
    assert repeat["status"] == TOOL_STATUS_ALREADY_FORWARDED, repeat
    assert repeat["already_forwarded"] is True, repeat
    assert repeat["target_message_id"] == "m-delivered-42", "已转发结果必须带回既有目标消息 ID"
    assert "未发送源消息或评论" in str(repeat["reason"]), repeat
    assert restarted_ctx.send.calls == [], "已投递记录必须阻止源消息与评论重发"
    assert restart_step.entries == []


def test_成功记录不因容量或重开账本被静默清除(tmp_path: Path) -> None:
    # 结构性守卫：账本不提供删除 / 清理 / TTL 接口，成功记录只增不减
    public_methods = [name for name in dir(DeliveryLedger) if not name.startswith("_")]
    assert not any(
        keyword in name.lower()
        for name in public_methods
        for keyword in ("delete", "purge", "cleanup", "ttl")
    ), f"账本不得提供删除或 TTL 接口: {public_methods}"

    path = tmp_path / LEDGER_FILE_NAME
    ledger = open_ledger(path, clock=FakeClock())
    assert isinstance(ledger, DeliveryLedger)
    first = ledger_key(message_id="m-first")
    assert ledger.reserve(first).granted is True
    assert ledger.mark_sent(first, "m-delivered-first") is True
    assert ledger.record_comment(first, status=STATUS_SENT, message_id="c-first") is True
    first_record = ledger.get(first)
    assert first_record is not None
    created_at = first_record.created_at
    for index in range(200):
        bulk = ledger_key(message_id=f"m-bulk-{index}")
        assert ledger.reserve(bulk).granted is True
        assert ledger.mark_sent(bulk, f"m-delivered-{index}") is True
    ledger.close()

    # 重开账本（其前已写入 200 条新记录）：旧记录与评论结果仍在，且没有被清理重写
    reopened = open_ledger(path, clock=FakeClock(start=10 * 365 * 24 * 3600))
    assert isinstance(reopened, DeliveryLedger)
    record = reopened.get(first)
    assert record is not None and record.status == STATUS_SENT, (
        "成功记录不得按容量或重开账本静默清除"
    )
    assert (record.target_message_id, record.comment_status) == ("m-delivered-first", STATUS_SENT)
    assert record.created_at == created_at, "旧记录不得被清理后重写（创建时间必须原样保留）"
    oldest_bulk = reopened.get(ledger_key(message_id="m-bulk-0"))
    assert oldest_bulk is not None and oldest_bulk.status == STATUS_SENT, (
        "既有记录不得在加入新记录后被按容量静默清除"
    )


# --------------------------------------------------------------------------- 4.4


def test_明确未发送的失败可重试(tmp_path: Path) -> None:
    failing = RecordingSendStep(status=STATUS_FAILED, reason="宿主发送能力明确返回未发送")
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=failing)
    prepare_source(ctx)

    first, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert first["success"] is False, first
    assert first["status"] == STATUS_FAILED, first
    assert first["sent"] is False and first["delivered"] is False, first
    assert "本次未发送任何消息" in str(first["reason"]), first
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_FAILED

    # 明确未发送：允许后续显式调用重试（换一个能成功的发送步骤）
    retry_step = RecordingSendStep(target_message_id="m-delivered-retry")
    retried, retried_ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        send_source=retry_step,
    )
    prepare_source(retried_ctx)

    second, _ = run_bound_forward(retried, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert second["success"] is True, second
    assert second["target_message_id"] == "m-delivered-retry", second
    assert len(retried_ctx.send.calls) == 1, "重试必须真正再次进入源发送"
    assert open_test_ledger(tmp_path).get(ledger_key()).status == STATUS_SENT


def test_超时或丢失回执返回待核实且阻止重发(tmp_path: Path) -> None:
    for label, reason in (("超时", "发送等待超时"), ("回执丢失", "发送回执丢失")):
        step = RecordingSendStep(status=STATUS_UNKNOWN, reason=reason)
        data_dir = tmp_path / label
        plugin, ctx = make_plugin(data_dir, config=enabled_config(), send_source=step)
        prepare_source(ctx)

        first, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

        assert first["success"] is False, (label, first)
        assert first["status"] == STATUS_UNKNOWN, (label, first)
        assert first["sent"] is False, (label, "结果未知不得伪报成功", first)
        assert "待核实" in str(first["reason"]), (label, first)
        assert len(ctx.send.calls) == 1, (label, first)
        record = open_test_ledger(data_dir).get(ledger_key())
        assert record is not None and record.status == STATUS_UNKNOWN, (label, record)

        # 阻止重发：显式再调用一次也不得再次进入源发送
        repeat, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
        assert repeat["success"] is False, (label, repeat)
        assert repeat["status"] == STATUS_UNKNOWN, (label, repeat)
        assert len(ctx.send.calls) == 1, (label, "待核实记录必须阻止重发")


def test_未识别发送状态一律按待核实处理且阻止重发(tmp_path: Path) -> None:
    """契约外的状态字面量（空串、大小写不符、超时/取消等）不得降级为可重试失败。

    发送步骤契约只定义 `sent` / `failed` / `unknown`；任何未识别取值都按「可能已
    发送」保守处理（待核实并阻止重发），不当作「明确未发送」。
    """

    for index, status in enumerate(("", "seNd", "cancelled", "timeout")):
        data_dir = tmp_path / f"unrecognized-{index}"
        step = RecordingSendStep(status=status, reason=f"发送步骤返回 {status!r}")
        plugin, ctx = make_plugin(data_dir, config=enabled_config(), send_source=step)
        prepare_source(ctx)

        first, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

        assert first["success"] is False, (status, first)
        assert first["status"] == STATUS_UNKNOWN, (status, "未识别状态必须按待核实兜底", first)
        assert first["sent"] is False and first["delivered"] is False, (
            status,
            "未识别状态不得声称发送成功",
            first,
        )
        assert "待核实" in str(first["reason"]), (status, first)
        record = open_test_ledger(data_dir).get(ledger_key())
        assert record is not None and record.status == STATUS_UNKNOWN, (status, record)

        repeat, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
        assert repeat["status"] == STATUS_UNKNOWN, (status, repeat)
        assert len(step.entries) == 1, (status, "未识别状态必须阻止重发", step.entries)


def test_终态登记失败时不得宣称可重试失败(tmp_path: Path) -> None:
    """明确未发送但失败状态未能落盘时，账本里仍是发送中占位（重启会归并为待核实）。

    因此对外不得降级成「可重试的明确失败」（第 4 组评审遗留 #8）：如实按待核实
    报告，并继续阻止重复发送。
    """

    flaky: list[FailingLedger] = []

    def flaky_open(path: Path) -> Any:
        ledger = open_ledger(path, clock=FakeClock())
        assert isinstance(ledger, DeliveryLedger)
        double = FailingLedger(ledger, failing=("mark_failed",))
        flaky.append(double)
        return double

    step = RecordingSendStep(status=STATUS_FAILED, reason="宿主明确未发送")
    plugin, ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        send_source=step,
        open_ledger=flaky_open,
    )
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["status"] == STATUS_UNKNOWN, ("登记失败不得宣称可重试", result)
    assert result["sent"] is False, result
    assert "待核实" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert flaky[0].failed_calls == ["mark_failed"], flaky[0].failed_calls
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_SENDING, (
        "占位不得被当成终态（重启后会归并为待核实）",
        record,
    )

    # 后续显式调用被未决占位阻止：不会假装可重试而重复发送
    repeat, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert repeat["status"] == STATUS_SENDING, repeat
    assert len(step.entries) == 1, step.entries


def test_失败与待核实都不启动后台自动重试(tmp_path: Path) -> None:
    cases: list[tuple[Any, Any, FakeClock, dict[str, Any]]] = []
    for index, status in enumerate((STATUS_FAILED, STATUS_UNKNOWN)):
        step = RecordingSendStep(status=status, reason=f"{status} 的发送结果")
        clock = FakeClock()
        plugin, ctx = make_plugin(
            tmp_path / f"case-{index}",
            config=enabled_config(),
            send_source=step,
            clock=clock,
        )
        prepare_source(ctx)
        cases.append((plugin, ctx, clock, issue_source_call(plugin, message_id="m-source-1")))

    async def scenario() -> list[int]:
        extra_sends: list[int] = []
        for plugin, ctx, clock, arguments in cases:
            await plugin.forward_message(**arguments)
            baseline = len(ctx.send.calls)
            clock.advance(6 * 3600)
            for _ in range(20):
                await asyncio.sleep(0)
            lingering = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]
            assert lingering == [], f"不得留下后台重试任务: {lingering}"
            extra_sends.append(len(ctx.send.calls) - baseline)
        return extra_sends

    assert asyncio.run(scenario()) == [0, 0], "明确失败与结果未知都不得启动后台自动重试"


# --------------------------------------------------------------------------- 4.5


def test_遗留sending重启后进入待核实且不盲目重发(tmp_path: Path) -> None:
    # 崩溃前进程：占位成功但还没落终态就中断（连接被直接关闭）
    crashed = open_test_ledger(tmp_path)
    assert crashed.reserve(ledger_key()).granted is True
    crashed.close()

    step = RecordingSendStep()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == STATUS_UNKNOWN, result
    assert "待核实" in str(result["reason"]), result
    assert ctx.send.calls == [] and step.entries == [], "遗留占位不得被盲目重发"
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_UNKNOWN, (
        "遗留 sending 必须归并为待核实，而不是可安全重试的失败"
    )


def test_启动恢复失败时告警且不静默当作零条(tmp_path: Path) -> None:
    # 1) 健康空账本：恢复零条，不产生恢复告警（与失败可区分）
    healthy_plugin, healthy_ctx = make_plugin(
        tmp_path / "healthy",
        config=enabled_config(),
        send_source=RecordingSendStep(),
    )
    asyncio.run(healthy_plugin.on_load())
    assert healthy_ctx.logger.texts("warning") == [], (
        f"空账本恢复零条不得产生恢复告警: {healthy_ctx.logger.records}"
    )

    # 2) 遗留 sending + 恢复失败：必须告警一次，且方向仍然安全（继续阻止重发）
    crashed = open_test_ledger(tmp_path)
    assert crashed.reserve(ledger_key()).granted is True
    crashed.close()

    flaky: list[FailingLedger] = []

    def flaky_open(path: Path) -> Any:
        ledger = open_ledger(path, clock=FakeClock())
        assert isinstance(ledger, DeliveryLedger)
        double = FailingLedger(ledger, failing=("recover_interrupted",))
        flaky.append(double)
        return double

    step = RecordingSendStep()
    plugin, ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        send_source=step,
        open_ledger=flaky_open,
    )
    prepare_source(ctx)

    asyncio.run(plugin.on_load())

    assert any("恢复" in text for text in ctx.logger.texts("warning")), (
        f"恢复失败必须告警，不能与「零条」不可区分: {ctx.logger.records}"
    )
    assert flaky[0].failed_calls == ["recover_interrupted"], "恢复失败必须被如实识别，不得当作零条"

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert result["status"] == STATUS_SENDING, result
    assert ctx.send.calls == [] and step.entries == [], "恢复失败时遗留 sending 仍必须阻止重发"


def test_发送步骤被中断时保留在途占位不误判失败(tmp_path: Path) -> None:
    async def crashing_step(prepared: Any) -> Any:
        del prepared
        raise asyncio.CancelledError

    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=crashing_step)
    prepare_source(ctx)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.forward_message(**issue_source_call(plugin, message_id="m-source-1")))

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_SENDING, (
        "中断不得被误记成明确未发送的失败"
    )

    step = RecordingSendStep()
    restarted, restarted_ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
    prepare_source(restarted_ctx)
    repeat, _ = run_bound_forward(restarted, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert repeat["status"] == STATUS_UNKNOWN, repeat
    assert restarted_ctx.send.calls == [], "中断遗留的投递不得盲目重发"


def test_发送成功后落盘失败不发送评论并保留已知回执(tmp_path: Path) -> None:
    flaky: list[FailingLedger] = []

    def flaky_open(path: Path) -> Any:
        ledger = open_ledger(path, clock=FakeClock())
        assert isinstance(ledger, DeliveryLedger)
        double = FailingLedger(ledger, failing=("mark_sent", "mark_unknown"))
        flaky.append(double)
        return double

    step = RecordingSendStep(target_message_id="m-delivered-lost")
    plugin, ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        send_source=step,
        open_ledger=flaky_open,
    )
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    assert result["status"] == STATUS_UNKNOWN, result
    assert result["sent"] is True and result["delivered"] is True, result
    assert result["target_message_id"] == "m-delivered-lost", "已知回执必须保留用于报告"
    assert "未发送评论" in str(result["reason"]), result
    assert len(ctx.send.calls) == 1, "落盘失败后不得继续发送评论"
    assert flaky[0].failed_calls == ["mark_sent", "mark_unknown"], (
        "落盘失败后只应尝试补记待核实，不得开始评论段"
    )

    # 重启后仍不盲目重发：遗留 sending 在打开账本时归并为待核实
    restarted, restarted_ctx = make_plugin(
        tmp_path,
        config=enabled_config(),
        send_source=RecordingSendStep(),
    )
    prepare_source(restarted_ctx)
    repeat, _ = run_bound_forward(restarted, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert repeat["success"] is False, repeat
    assert repeat["status"] == STATUS_UNKNOWN, repeat
    assert restarted_ctx.send.calls == []
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_UNKNOWN, record
    assert record.comment_status == "", "落盘失败后不得留下评论结果"


def test_发送后落盘失败与占位未命中的措辞可区分(tmp_path: Path) -> None:
    """两类「状态未登记」都按待核实报告，但原因必须区分存储失败与占位未命中。"""

    def failing_opener(mode: str) -> Any:
        def opener(path: Path) -> Any:
            ledger = open_ledger(path, clock=FakeClock())
            assert isinstance(ledger, DeliveryLedger)
            return FailingLedger(
                ledger,
                failing=("mark_sent", "mark_unknown"),
                failure_mode=mode,
            )

        return opener

    cases = (
        ("存储失败", "raise", "存储"),
        ("占位未命中", "false", "占位"),
    )
    for label, mode, expected in cases:
        data_dir = tmp_path / label
        step = RecordingSendStep(target_message_id=f"m-delivered-{mode}")
        plugin, ctx = make_plugin(
            data_dir,
            config=enabled_config(),
            send_source=step,
            open_ledger=failing_opener(mode),
        )
        prepare_source(ctx)

        result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

        assert result["status"] == STATUS_UNKNOWN, (label, result)
        assert result["sent"] is True and result["delivered"] is True, (label, result)
        assert result["target_message_id"] == f"m-delivered-{mode}", (label, result)
        reason = str(result["reason"])
        assert "待核实" in reason and "未发送评论" in reason, (label, result)
        assert expected in reason, (label, f"必须区分失败原因（{expected}）", result)


# --------------------------------------------------------------------------- 4.6


def test_账本损坏时关闭发送且不覆盖状态数据(tmp_path: Path) -> None:
    ledger_path = tmp_path / LEDGER_FILE_NAME
    ledger_path.write_bytes("这不是一个 SQLite 数据库".encode("utf-8") + b"\x00\xff" * 8)
    broken_bytes = ledger_path.read_bytes()
    step = RecordingSendStep()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
    prepare_source(ctx)

    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

    assert result["success"] is False, result
    reason = str(result["reason"])
    assert "账本" in reason and "损坏" in reason, ("必须输出明确原因", result)
    assert "本次未发送任何消息" in reason, result
    assert ctx.send.calls == [] and step.entries == [], "账本损坏时不得发送"
    assert any("账本" in text for text in ctx.logger.texts("error")), (
        f"必须记录可定位的错误日志: {ctx.logger.records}"
    )

    # 重复调用仍然关闭发送；损坏的状态数据必须原样保留（不覆盖、不删除、不当空库）
    repeat, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert repeat["success"] is False and "账本" in str(repeat["reason"]), repeat
    assert ctx.send.calls == [] and step.entries == []
    assert ledger_path.exists(), "不得删除损坏的状态数据"
    assert ledger_path.read_bytes() == broken_bytes, "不得覆盖损坏的状态数据"


def test_启动时发现损坏账本就报错并关闭发送(tmp_path: Path) -> None:
    ledger_path = tmp_path / LEDGER_FILE_NAME
    ledger_path.write_bytes("这不是一个 SQLite 数据库".encode("utf-8") + b"\x00\xff" * 8)
    step = RecordingSendStep()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
    prepare_source(ctx)

    asyncio.run(plugin.on_load())

    assert any("账本" in text for text in ctx.logger.texts("error")), (
        f"加载时必须记录明确原因: {ctx.logger.records}"
    )
    result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)
    assert result["success"] is False and "账本" in str(result["reason"]), result
    assert ctx.send.calls == [] and step.entries == []


def test_账本不可读时关闭发送(tmp_path: Path) -> None:
    ledger_path = tmp_path / LEDGER_FILE_NAME
    healthy = open_ledger(ledger_path, clock=FakeClock())
    assert isinstance(healthy, DeliveryLedger)
    healthy.close()
    ledger_path.chmod(0o000)
    try:
        step = RecordingSendStep()
        plugin, ctx = make_plugin(tmp_path, config=enabled_config(), send_source=step)
        prepare_source(ctx)

        result, _ = run_bound_forward(plugin, message_id="m-source-1", target_group_id=TARGET_GROUP_ID)

        assert result["success"] is False, result
        reason = str(result["reason"])
        assert "账本" in reason, ("必须说明是账本问题", result)
        assert "不可读" in reason or "不可写" in reason, ("原因必须可定位到不可读写", result)
        assert ctx.send.calls == [] and step.entries == [], "账本不可读时不得发送"
    finally:
        ledger_path.chmod(0o600)
