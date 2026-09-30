"""第 7 组：生命周期、取消与热更新（7.1–7.4）。

用例只经 SDK 生命周期入口（`on_load` / `on_unload` / `on_config_update`）、观察 Hook
输入事件与 `forward_message` 公共入口驱动，不直接操纵插件的私有任务集合，也不访问
真实群。

契约（design D7 / spec「独立插件与路径开关」「结果与生命周期」）：插件持有并等待
自己创建的**全部**后台任务；卸载或关闭总开关时取消尚未发送的工作并保留已发生发送
的事实；卸载期间新到达或即将创建的发送工作必须被拦截（预检返回后仍复检卸载标记）；
等待有上限，超时必须告警且告警可定位；白名单热更新在源发送前与评论发送前重新检查
权限，不依赖开始时的旧配置快照；自动入口开关只停止自动唤起，不禁用工具；不操作
其他插件的任务。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import plugin as plugin_module
from delivery_ledger import STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_UNKNOWN
from forward_delivery import (
    TOOL_STATUS_ALREADY_FORWARDED,
    TOOL_STATUS_REJECTED,
    SourceSendResult,
)

from tests.support import (
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    FakeClock,
    RecordingSendStep,
    apply_config_update,
    enabled_config,
    forward_node,
    forward_segment,
    intent_criteria_region,
    issue_source_call,
    ledger_key,
    make_plugin,
    merged_forward_message,
    open_test_ledger,
    prepare_source,
    received_message,
    run_bound_forward,
    source_stream,
    text_segment,
    wait_until,
)

# --------------------------------------------------------------------------- 7.1


def _prepare_comment_wait_unload(tmp_path: Path) -> tuple[Any, FakeClock, Any, dict[str, Any]]:
    """装配「评论等待中卸载」场景：返回（插件, 注入时钟, 上下文, 工具参数）。

    可信来源绑定（`issue_source_call`）内部自建事件循环，因此装配必须发生在测试的
    事件循环**之外**；7.1 与 7.4 的该场景共用本装配，避免两份逐行相同的场景代码
    各自漂移。
    """

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="等待期间卸载")
    return plugin, clock, ctx, arguments


async def _drive_comment_wait_unload(
    plugin: Any,
    clock: FakeClock,
    arguments: dict[str, Any],
) -> asyncio.Task[Any]:
    """推动场景：源发送成功落账并进入 0.5 秒等待，随后卸载；返回工具调用任务。

    返回时任务已被卸载取消，调用方在同一事件循环内继续断言取消、日志与账本事实。
    """

    await plugin.on_load()
    task = asyncio.create_task(plugin.forward_message(**arguments))
    await wait_until(lambda: clock.sleeps == [0.5])
    await plugin.on_unload()
    return task


def test_卸载取消尚未发送的自动等待并等待任务退出(tmp_path: Path) -> None:
    """卸载必须取消待执行的自动等待、等待本插件任务退出，且不碰其他插件的任务。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    prepare_source(ctx, message_id="m-broadcast-1")
    ctx.message.store = {}
    payload = merged_forward_message(
        message_id="m-broadcast-1",
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )
    foreign_release = asyncio.Event()
    foreign_finished: list[str] = []

    async def foreign_work() -> None:
        """另一插件（或宿主）自己的任务：本插件的卸载不得取消它。"""

        await foreign_release.wait()
        foreign_finished.append("done")

    async def scenario() -> None:
        await plugin.on_load()
        await plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(clock.sleeps))
        assert len(ctx.message.calls) == 1, "自动等待先做一次可读性确认后进入有界等待"

        foreign = asyncio.create_task(foreign_work())
        await plugin.on_unload()

        assert not foreign.done(), "卸载不得取消其他插件的任务"
        clock.advance(60.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert ctx.maisaka.proactive.calls == [], "卸载后不得再提供任何判断机会"
        assert len(ctx.message.calls) == 1, "被取消的自动等待不得继续轮询宿主"
        assert not any("未退出" in text for text in ctx.logger.texts("warning")), (
            "取消在卸载等待上限内生效，不得留下「未退出」告警",
            ctx.logger.records,
        )
        assert "已卸载" in "\n".join(ctx.logger.texts("info")), (
            "卸载必须等待被取消的任务退出并留下完成记录",
            ctx.logger.records,
        )

        foreign_release.set()
        await foreign
        assert foreign_finished == ["done"], "其他插件的任务在卸载后应能正常完成"

    asyncio.run(scenario())


def test_卸载取消在途投递并等待任务退出(tmp_path: Path) -> None:
    """卸载取消评论等待中的投递：工具调用被取消，卸载后不再有孤立任务发送评论。"""

    plugin, clock, ctx, arguments = _prepare_comment_wait_unload(tmp_path)

    async def scenario() -> None:
        task = await _drive_comment_wait_unload(plugin, clock, arguments)

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.done(), "卸载必须取消并等待在途投递任务退出（不得让它在后台继续）"
        assert task.cancelled(), "被卸载取消的投递任务应如实表现为取消"
        assert not any("未退出" in text for text in ctx.logger.texts("warning")), (
            "被取消的投递任务在卸载等待上限内退出，不得留下「未退出」告警",
            ctx.logger.records,
        )
        assert "已卸载" in "\n".join(ctx.logger.texts("info")), (
            "卸载必须等待被取消的投递任务退出并留下完成记录",
            ctx.logger.records,
        )

        clock.advance(1.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(ctx.send.calls) == 1, "卸载后不得再发送评论"

    asyncio.run(scenario())

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), (
        "已成功发送的源消息事实不得因卸载回滚",
        record,
    )


class _StubbornSendStep(RecordingSendStep):
    """替身源发送步骤：吞掉一次取消后继续等待，释放后才按真实回执返回。

    模拟宿主调用悬挂（取消未在最近的 await 点生效）：`_drain_tasks` 的上限等不到它
    退出，只能告警返回；这条**已在途**的调用随后仍可能完成，因此不得被当成「未发送」。
    """

    def __init__(self, *, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(status=STATUS_SENT, target_message_id="m-target-late")
        self.entered = entered
        self.release = release

    async def __call__(self, prepared: Any) -> SourceSendResult:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
        return await super().__call__(prepared)


def test_卸载等待超时告警可定位且不再发起新的发送(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主调用吞掉取消时：卸载按上限返回、超时告警可定位，其后零新发送。

    上限在这里被缩短到 0.05 秒：本用例验证的是「等待有界 + 告警不静默 + 告警可定位」
    这一分支，而不是 5 秒这个字面值；不缩短就要真实等待 5 秒。
    """

    monkeypatch.setattr(plugin_module, "TASK_EXIT_WAIT_SECONDS", 0.05)
    entered = asyncio.Event()
    release = asyncio.Event()
    plugin, ctx = make_plugin(
        tmp_path,
        clock=FakeClock(),
        config=enabled_config(),
        send_source=_StubbornSendStep(entered=entered, release=release),
    )
    prepare_source(ctx)
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="卸载后不得补发")

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await entered.wait()

        await plugin.on_unload()

        warnings = "\n".join(ctx.logger.texts("warning"))
        assert "未退出" in warnings, (
            "等待上限内没退出的任务必须告警，不得静默留下孤立任务",
            ctx.logger.records,
        )
        assert "m-source-1" in warnings and TARGET_GROUP_ID in warnings, (
            "超时告警必须能定位源消息与目标群",
            ctx.logger.records,
        )
        assert "已卸载" in "\n".join(ctx.logger.texts("info")), (
            "等待超时后卸载仍必须返回并留下完成记录",
            ctx.logger.records,
        )
        assert not task.done(), "吞掉取消的在途发送仍在跑（宿主调用可能仍完成）"

        release.set()
        for _ in range(200):
            if task.done():
                break
            await asyncio.sleep(0)
        result = await task
        assert [call["capability"] for call in ctx.send.calls] == ["send.text"], (
            "卸载返回后只允许已经在途的源发送完成，不得再发起任何新的发送（不补发评论）",
            ctx.send.calls,
        )
        assert result["status"] == STATUS_UNKNOWN, result
        assert result["target_message_id"] == "m-target-late", result
        return result

    asyncio.run(scenario())


def test_卸载后到达的工具调用不再发送(tmp_path: Path) -> None:
    """卸载后到达的调用（宿主仍在沿旧实例分发）不得再发送，并给出可定位原因。"""

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)
    arguments = issue_source_call(plugin, message_id="m-source-1")

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        await plugin.on_unload()
        return await plugin.forward_message(**arguments)

    result = asyncio.run(scenario())

    assert result["status"] == TOOL_STATUS_REJECTED, result
    assert "已卸载" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == []
    assert ctx.chat.calls == [], "卸载后不得再查询宿主会话"


def test_预检期间卸载时不再创建发送任务(tmp_path: Path) -> None:
    """预检期间发生卸载：复检必须挡在创建任务与发送之前，不重新打开账本、不写占位。

    红：缺少复检时，调用会在卸载之后重新打开账本、创建一份本不属于本轮卸载等待的
    发送任务，并留下一条投递占位（`ledger.get` 非空）。
    """

    plugin, ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(ctx)
    arguments = issue_source_call(plugin, message_id="m-source-1")
    original_open_session = ctx.chat.open_session

    async def unloading_open_session(*args: Any, **kwargs: Any) -> Any:
        """预检期间（解析目标路由时）插件被卸载；预检本身仍会照常结束。"""

        await plugin.on_unload()
        return await original_open_session(*args, **kwargs)

    ctx.chat.open_session = unloading_open_session

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        return await plugin.forward_message(**arguments)

    result = asyncio.run(scenario())

    assert result["status"] == TOOL_STATUS_REJECTED, result
    assert "已卸载" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == []
    assert open_test_ledger(tmp_path).get(ledger_key()) is None, (
        "卸载后不得再打开账本或写入任何投递占位",
    )


# --------------------------------------------------------------------------- 7.2


def test_关闭总开关后待执行的自动唤起被取消(tmp_path: Path) -> None:
    """关闭总开关必须取消待执行自动工作：不得继续按旧配置快照轮询与唤起。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    ctx.message.sequences = {"m-broadcast-1": [None, None, None]}
    payload = merged_forward_message(
        message_id="m-broadcast-1",
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )
    disabled = enabled_config()
    disabled["plugin"] = {"enabled": False}

    async def scenario() -> None:
        await plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(clock.sleeps))
        assert len(ctx.message.calls) == 1, "自动等待先做一次可读性确认后进入有界等待"

        await apply_config_update(plugin, disabled)

        clock.advance(60.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert ctx.maisaka.proactive.calls == [], "总开关关闭后不得再提供判断机会"
        assert len(ctx.message.calls) == 1, "待执行的自动工作必须被取消，不得继续轮询宿主"

    asyncio.run(scenario())


def test_仅关闭自动入口时待执行唤起被取消且工具仍可发送(tmp_path: Path) -> None:
    """自动入口开关只停自动唤起：待执行唤起被取消，总开关开启时工具照常发送。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    prepare_source(ctx, message_id="m-broadcast-1")
    ctx.message.sequences = {"m-broadcast-1": [None, None, None]}
    payload = merged_forward_message(
        message_id="m-broadcast-1",
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )
    auto_off = enabled_config()
    auto_off["auto"] = {"enabled": False}

    async def scenario() -> None:
        await plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(clock.sleeps))

        await apply_config_update(plugin, auto_off)

        clock.advance(60.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert ctx.maisaka.proactive.calls == [], "自动入口关闭后不得再自动唤起"
        assert len(ctx.message.calls) == 1, "待执行唤起必须被取消，不得继续轮询宿主"

    asyncio.run(scenario())

    ctx.message.sequences = {}
    result, _ = run_bound_forward(plugin, message_id="m-broadcast-1")

    assert result["status"] == STATUS_SENT, result
    assert [call["capability"] for call in ctx.send.calls] == ["send.text"], (
        "自动入口关闭只停止自动唤起，不禁用总开关开启时的合法工具调用",
        ctx.send.calls,
    )


# --------------------------------------------------------------------------- 7.3


def test_源发送前权限撤销时零发送并释放占位(tmp_path: Path) -> None:
    """预检通过后、实际源发送前撤销权限：一次发送都不能发生，占位按明确未发送释放。"""

    cases: list[tuple[str, dict[str, Any]]] = [
        ("撤销目标白名单", enabled_config(target_groups=())),
        ("撤销源白名单", enabled_config(source_groups=())),
        ("关闭总开关", {**enabled_config(), "plugin": {"enabled": False}}),
    ]
    for label, revoked in cases:
        clock = FakeClock()
        step = RecordingSendStep()
        plugin, ctx = make_plugin(
            tmp_path / label,
            clock=clock,
            config=enabled_config(),
            send_source=step,
        )
        prepare_source(ctx)
        original_open_session = ctx.chat.open_session

        async def revoking_open_session(*args: Any, **kwargs: Any) -> Any:
            """预检期间（解析目标路由时）发生配置热更新。"""

            await apply_config_update(plugin, revoked)
            return await original_open_session(*args, **kwargs)

        ctx.chat.open_session = revoking_open_session

        result, _ = run_bound_forward(plugin, message_id="m-source-1")

        assert ctx.send.calls == [] and step.entries == [], (
            label,
            "预检后撤销权限时不得进入源发送",
            ctx.send.calls,
        )
        assert result["status"] == TOOL_STATUS_REJECTED, (label, result)
        assert "本次未发送任何消息" in str(result["reason"]), (label, result)
        record = open_test_ledger(tmp_path / label).get(ledger_key())
        assert record is not None and record.status == STATUS_FAILED, (
            label,
            record,
            "撤销发生在发送前：占位必须按可观为「明确未发送」释放（可显式重试）",
        )
        assert record.target_message_id == "" and record.comment_status == "", (label, record)


def test_白名单热更新取消已失效的待执行自动唤起(tmp_path: Path) -> None:
    """来源群或目标白名单被撤销后，待执行的自动唤起立即取消（不看旧配置快照）。"""

    cases: list[tuple[str, dict[str, Any]]] = [
        ("撤销目标白名单", enabled_config(target_groups=())),
        ("撤销源白名单", enabled_config(source_groups=())),
    ]
    for label, revoked in cases:
        clock = FakeClock()
        plugin, ctx = make_plugin(tmp_path / label, clock=clock, config=enabled_config())
        ctx.message.sequences = {"m-broadcast-1": [None, None, None]}
        payload = merged_forward_message(
            message_id="m-broadcast-1",
            session_id=SOURCE_SESSION_ID,
            group_id=SOURCE_GROUP_ID,
        )

        async def scenario() -> None:
            await plugin.observe_received_message(**payload)
            await wait_until(lambda: bool(clock.sleeps))

            await apply_config_update(plugin, revoked)

            clock.advance(60.0)
            for _ in range(50):
                await asyncio.sleep(0)
            assert ctx.maisaka.proactive.calls == [], (label, "白名单撤销后不得再唤起判断")
            assert len(ctx.message.calls) == 1, (
                label,
                "已失效的待执行工作必须被取消，不得继续轮询宿主",
            )

        asyncio.run(scenario())


# --------------------------------------------------------------------------- 7.4


def test_评论等待中卸载时不发送评论并保留源成功记录(tmp_path: Path) -> None:
    """0.5 秒等待中卸载：不发送评论，源消息的 sent 记录与真实目标消息 ID 保留。"""

    plugin, clock, ctx, arguments = _prepare_comment_wait_unload(tmp_path)

    async def scenario() -> None:
        task = await _drive_comment_wait_unload(plugin, clock, arguments)

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.cancelled(), "卸载必须取消评论等待中的投递"
        assert not any("未退出" in text for text in ctx.logger.texts("warning")), (
            "评论等待中的投递在卸载等待上限内退出，不得留下「未退出」告警",
            ctx.logger.records,
        )
        assert "已卸载" in "\n".join(ctx.logger.texts("info")), (
            "卸载必须等待被取消的投递任务退出并留下完成记录",
            ctx.logger.records,
        )
        clock.advance(1.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(ctx.send.calls) == 1, "卸载后不得再发送评论"

    asyncio.run(scenario())

    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), (
        "源消息已成功发送：事实与真实目标消息 ID 必须保留",
        record,
    )
    assert (record.comment_status, record.comment_message_id) == ("", ""), (
        "被取消的评论不得留下任何结果",
        record,
    )

    # 重启后重复调用：不重发源消息，也不自动补发评论
    restarted, restarted_ctx = make_plugin(tmp_path, config=enabled_config(), clock=FakeClock())
    prepare_source(restarted_ctx)
    repeat, _ = run_bound_forward(restarted, message_id="m-source-1", comment="不补发")
    assert repeat["status"] == TOOL_STATUS_ALREADY_FORWARDED, repeat
    assert restarted_ctx.send.calls == [], "重复调用不得重发源消息或补发评论"


def test_源发送进行中卸载时保持待核实不降为可重试(tmp_path: Path) -> None:
    """源发送进行中被卸载取消：结果不确定，占位保持待核实，重启也不得盲目重发。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx, raw_message=[forward_segment(forward_node())])
    ctx.send.forward_results = [{"sent": True, "message_id": "m-target-1"}]
    arguments = issue_source_call(plugin, message_id="m-source-1")

    async def scenario() -> None:
        await plugin.on_load()
        ctx.send.hold = asyncio.Event()
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: len(ctx.send.calls) == 1)

        await plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.cancelled(), "卸载必须取消在途源发送"
        record = open_test_ledger(tmp_path).get(ledger_key())
        assert record is not None and record.status == STATUS_SENDING, (
            "发送进行中被取消：结果不确定，必须保持待核实占位而不是可重试失败",
            record,
        )
        assert record.target_message_id == "", record

    asyncio.run(scenario())

    restarted, restarted_ctx = make_plugin(tmp_path, config=enabled_config())
    prepare_source(restarted_ctx, raw_message=[forward_segment(forward_node())])
    asyncio.run(restarted.on_load())
    repeat, _ = run_bound_forward(restarted, message_id="m-source-1")
    assert repeat["status"] == STATUS_UNKNOWN, repeat
    assert restarted_ctx.send.calls == [], "待核实记录必须阻止重发，不得降为可安全重试"


def test_评论等待中撤销目标权限时不发送评论并保留源成功记录(tmp_path: Path) -> None:
    """热更新撤销目标权限：评论不发送，源成功记录与目标消息 ID 保留，结果为部分成功。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, config=enabled_config(), clock=clock)
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="等待期间被撤销")
    revoked = enabled_config(target_groups=())

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: clock.sleeps == [0.5])
        await apply_config_update(plugin, revoked)
        clock.advance(0.5)
        return await task

    result = asyncio.run(scenario())

    assert result["status"] == "partial", result
    assert result["sent"] is True and result["target_message_id"] == "m-target-src", result
    assert result["comment_status"] == STATUS_FAILED, result
    assert "未发送" in str(result["reason"]), result
    assert len(ctx.send.calls) == 1, ("撤销目标权限后不得发送评论", ctx.send.calls)
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), record
    assert record.comment_status == STATUS_FAILED, record


# -------------- 本变更第 5 组：自动判断的热更新与卸载边界（5.4–5.7）
# 共用 seam：注册的 `chat.receive.after_process` 观察 Hook、`CourierPlugin.on_config_update()`
# 与 `on_unload()`，必要时串接可信来源绑定 Hook → `forward_message()`。热更新统一由
# `apply_config_update` 按宿主顺序驱动（先注入配置，再调用生命周期）；异步悬停只施加在
# `ctx.message.get_by_id`、`ctx.chat.get_all_streams`、`ctx.maisaka.proactive.trigger`
# 与发送能力替身上，不替换生产协作者。


def test_等待期间移除一类候选只取消该类判断(tmp_path: Path) -> None:
    """5.4：并存的不同类型候选中，移除一类只让该类不再提交，其他合法候选继续。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path,
        clock=clock,
        config=enabled_config(message_types=("text", "forward")),
    )
    text_id, forward_id = "m-wait-text", "m-wait-forward"
    prepare_source(ctx, message_id=forward_id, raw_message=[forward_segment(forward_node())])
    ctx.message.sequences = {forward_id: [None]}  # 卡片先未就绪一次，等待期间发生热更新
    text_payload = received_message(message_id=text_id, raw_message=[text_segment("普通正文")])
    forward_payload = received_message(
        message_id=forward_id,
        raw_message=[forward_segment(forward_node())],
    )

    async def scenario() -> None:
        await plugin.observe_received_message(**text_payload)
        await plugin.observe_received_message(**forward_payload)
        await wait_until(lambda: len(clock.sleeps) == 2)
        assert len(ctx.message.calls) == 2, "两条候选各做一次可读性确认后进入有界等待"

        await apply_config_update(plugin, enabled_config(message_types=("forward",)))

        clock.advance(0.5)
        for _ in range(50):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    submitted = [call["metadata"]["source_message_id"] for call in ctx.maisaka.proactive.calls]
    assert submitted == [forward_id], "移除文字类型只让文字候选不再提交，卡片候选照常继续"
    queries = [call["message_id"] for call in ctx.message.calls]
    assert queries.count(text_id) == 1, "失效的文字候选被取消后不得继续轮询宿主"
    assert queries.count(forward_id) == 2, "仍合法的卡片候选继续等待并提交"


_WAIT_REVOCATIONS: dict[str, dict[str, Any]] = {
    "空候选列表": enabled_config(message_types=()),
    "关闭自动入口": {**enabled_config(), "auto": {"enabled": False}},
    "撤销来源白名单": enabled_config(source_groups=()),
    "撤销目标白名单": enabled_config(target_groups=()),
}


def test_等待期间撤销开关白名单或清空候选时不再提交(tmp_path: Path) -> None:
    """5.4：空选择、关闭自动入口与撤销来源／目标权限都阻止相应判断。"""

    for label, revoked in _WAIT_REVOCATIONS.items():
        clock = FakeClock()
        plugin, ctx = make_plugin(tmp_path / label, clock=clock, config=enabled_config())
        message_id = "m-wait-revoked"
        prepare_source(ctx, message_id=message_id)
        ctx.message.store = {}
        payload = merged_forward_message(
            message_id=message_id,
            session_id=SOURCE_SESSION_ID,
            group_id=SOURCE_GROUP_ID,
        )

        async def scenario() -> None:
            await plugin.observe_received_message(**payload)
            await wait_until(lambda: bool(clock.sleeps))

            await apply_config_update(plugin, revoked)

            clock.advance(60.0)
            for _ in range(50):
                await asyncio.sleep(0)

        asyncio.run(scenario())

        assert ctx.maisaka.proactive.calls == [], (label, "失效后不得提交判断")
        assert len(ctx.message.calls) == 1, (label, "被取消的等待不得继续轮询宿主")


def test_只改标准不取消合法候选且后续提交使用新标准(tmp_path: Path) -> None:
    """5.4：只更新标准不取消仍合法的候选，等待结束后按最新标准提交。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path,
        clock=clock,
        config=enabled_config(share_criteria="旧标准：只分享技术话题"),
    )
    message_id = "m-criteria-wait"
    prepare_source(ctx, message_id=message_id)
    ctx.message.sequences = {message_id: [None]}
    payload = merged_forward_message(
        message_id=message_id,
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    async def scenario() -> None:
        await plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(clock.sleeps))

        await apply_config_update(plugin, enabled_config(share_criteria="新标准：只分享宠物照片"))

        clock.advance(0.5)
        for _ in range(50):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(ctx.maisaka.proactive.calls) == 1, "只改标准不得取消仍合法的候选"
    assert intent_criteria_region(ctx.maisaka.proactive.calls[0]["intent"]) == (
        "新标准：只分享宠物照片"
    ), "等待结束后的提交必须使用最新标准"


def test_热更新不整体取消在途投递(tmp_path: Path) -> None:
    """5.4：配置热更新只取消失效的自动判断，不整体取消已进入投递链的工作。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    prepare_source(ctx)
    ctx.send.text_results = [
        {"sent": True, "message_id": "m-target-src"},
        {"sent": True, "message_id": "m-target-comment"},
    ]
    arguments = issue_source_call(plugin, message_id="m-source-1", comment="热更新不取消投递")

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: clock.sleeps == [0.5])

        await apply_config_update(
            plugin,
            enabled_config(message_types=("text",), share_criteria="更新的自动分享标准"),
        )

        clock.advance(0.5)
        return await task

    result = asyncio.run(scenario())

    assert result["status"] == STATUS_SENT, result
    assert [call["capability"] for call in ctx.send.calls] == ["send.text", "send.text"], (
        "热更新不得取消评论等待中的在途投递",
        ctx.send.calls,
    )
    record = open_test_ledger(tmp_path).get(ledger_key())
    assert record is not None and record.status == STATUS_SENT, record


ADDED_TARGET_GROUP_ID = "30003"
"""名称查询期间新加入白名单的目标群（没有可用名称，意图只列群号）。"""

REMOVED_TARGET_GROUP_ID = "30004"
"""名称查询期间被移出白名单的目标群（不得从名称快照混进意图）。"""


def _hold_names_query(ctx: Any) -> tuple[asyncio.Event, asyncio.Event, list[Any]]:
    """让目标名称查询悬停在途（只在 `ctx.chat.get_all_streams` 边界注入）。

    返回（进入事件、放行事件、进入记录）：宿主请求已进入但尚未返回，供用例在查询
    期间修改配置；`entries` 用于断言提交前只发生一次名称查询，没有为补齐名称再查。
    """

    entered = asyncio.Event()
    release = asyncio.Event()
    entries: list[Any] = []
    original = ctx.chat.get_all_streams

    async def held_get_all_streams(*args: Any, **kwargs: Any) -> Any:
        entries.append((args, kwargs))
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    ctx.chat.get_all_streams = held_get_all_streams
    return entered, release, entries


def test_名称查询期间修改标准后按最新标准与目标提交(tmp_path: Path) -> None:
    """5.5：名称查询结束后重读最新配置：标准与目标都用最新值，查询只发生一次。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path,
        clock=clock,
        config=enabled_config(share_criteria="旧标准：只分享技术话题"),
    )
    message_id = "m-name-hold"
    prepare_source(ctx, message_id=message_id)
    ctx.chat.streams = [
        source_stream(),
        source_stream(session_id="s-target-a", group_id=TARGET_GROUP_ID),
    ]
    ctx.chat.streams[1]["group_name"] = "目标群甲"
    entered, release, entries = _hold_names_query(ctx)
    payload = merged_forward_message(
        message_id=message_id,
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    async def scenario() -> None:
        await plugin.on_load()
        await plugin.observe_received_message(**payload)
        await entered.wait()

        await apply_config_update(plugin, enabled_config(share_criteria="新标准：只分享宠物照片"))
        release.set()
        await wait_until(lambda: bool(ctx.maisaka.proactive.calls))
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(entries) == 1, "提交前不得再发起第二次名称查询"
    assert len(ctx.maisaka.proactive.calls) == 1
    intent = ctx.maisaka.proactive.calls[0]["intent"]
    assert intent_criteria_region(intent) == "新标准：只分享宠物照片", (
        "提交必须使用名称查询期间更新的最新标准"
    )
    assert f"- 目标群号 {TARGET_GROUP_ID}（群名显示文本：「目标群甲」）" in intent


def test_名称查询期间增删目标只提交最新合法目标(tmp_path: Path) -> None:
    """5.5：新增目标无名称时只列群号；已移除目标不得从名称查询快照混入。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path,
        clock=clock,
        config=enabled_config(target_groups=(TARGET_GROUP_ID, REMOVED_TARGET_GROUP_ID)),
    )
    message_id = "m-name-targets"
    prepare_source(ctx, message_id=message_id)
    ctx.chat.streams = [
        source_stream(),
        source_stream(session_id="s-target-a", group_id=TARGET_GROUP_ID),
        source_stream(session_id="s-target-b", group_id=REMOVED_TARGET_GROUP_ID),
    ]
    ctx.chat.streams[1]["group_name"] = "保留的目标群"
    ctx.chat.streams[2]["group_name"] = "将被移除的目标群"
    entered, release, entries = _hold_names_query(ctx)
    payload = merged_forward_message(
        message_id=message_id,
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    async def scenario() -> None:
        await plugin.on_load()
        await plugin.observe_received_message(**payload)
        await entered.wait()

        await apply_config_update(
            plugin,
            enabled_config(target_groups=(TARGET_GROUP_ID, ADDED_TARGET_GROUP_ID)),
        )
        release.set()
        await wait_until(lambda: bool(ctx.maisaka.proactive.calls))
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(entries) == 1, "提交前不得再发起第二次名称查询"
    intent = ctx.maisaka.proactive.calls[0]["intent"]
    target_lines = [line for line in intent.split("\n") if line.startswith("- 目标群号")]
    assert target_lines == [
        f"- 目标群号 {TARGET_GROUP_ID}（群名显示文本：「保留的目标群」）",
        f"- 目标群号 {ADDED_TARGET_GROUP_ID}",
    ], "只提交最新白名单：新增目标无名称只列群号，移除目标不得从旧快照混入"
    assert REMOVED_TARGET_GROUP_ID not in intent and "将被移除的目标群" not in intent


@pytest.mark.parametrize(
    ("label", "mutate"),
    (
        ("撤销候选类型", "revoke_type"),
        ("关闭总开关", "disable"),
        ("卸载", "unload"),
    ),
)
def test_名称查询期间失效时零提交(tmp_path: Path, label: str, mutate: str) -> None:
    """5.5：名称查询期间撤销类型、关闭开关或卸载后，候选零提交且不再查询。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path / label, clock=clock, config=enabled_config())
    message_id = "m-name-invalid"
    prepare_source(ctx, message_id=message_id)
    entered, release, entries = _hold_names_query(ctx)
    payload = merged_forward_message(
        message_id=message_id,
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    async def scenario() -> None:
        await plugin.on_load()
        driver = asyncio.create_task(plugin.observe_received_message(**payload))
        await entered.wait()

        if mutate == "revoke_type":
            await apply_config_update(plugin, enabled_config(message_types=("text",)))
        elif mutate == "disable":
            await apply_config_update(
                plugin,
                {**enabled_config(), "plugin": {"enabled": False}},
            )
        else:
            await plugin.on_unload()

        release.set()
        await driver
        for _ in range(50):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert ctx.maisaka.proactive.calls == [], (label, "失效后不得提交判断")
    assert len(entries) == 1, (label, "名称查询只发起一次，失效恢复后不得再查询")


def test_已提交判断不因配置更新被改写或补发(tmp_path: Path) -> None:
    """5.6：宿主已收到请求后更新类型或标准：不重写已提交意图、不补发第二次请求。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(
        tmp_path,
        clock=clock,
        config=enabled_config(share_criteria="旧标准：只分享技术话题"),
    )
    message_id = "m-committed"
    prepare_source(ctx, message_id=message_id)
    payload = merged_forward_message(
        message_id=message_id,
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    async def scenario() -> None:
        await plugin.on_load()
        original_trigger = ctx.maisaka.proactive.trigger
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_trigger(*args: Any, **kwargs: Any) -> Any:
            """宿主已收到请求（`calls` 已记录）但回执尚未返回给插件。"""

            response = await original_trigger(*args, **kwargs)
            entered.set()
            await release.wait()
            return response

        ctx.maisaka.proactive.trigger = held_trigger

        await plugin.observe_received_message(**payload)
        await entered.wait()
        assert len(ctx.maisaka.proactive.calls) == 1, "宿主已收到一次判断请求"

        await apply_config_update(
            plugin,
            enabled_config(message_types=("text",), share_criteria="新标准：只分享宠物照片"),
        )
        release.set()
        for _ in range(100):
            await asyncio.sleep(0)

        await plugin.observe_received_message(**payload)
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(ctx.maisaka.proactive.calls) == 1, "不得为新配置补发判断请求，也不得重放已登记候选"
    submitted_intent = ctx.maisaka.proactive.calls[0]["intent"]
    assert intent_criteria_region(submitted_intent) == "旧标准：只分享技术话题", (
        "已提交的意图保持提交时的标准原文，不被追溯改写"
    )
    logs = "\n".join(
        [*ctx.logger.texts("debug"), *ctx.logger.texts("info"), *ctx.logger.texts("warning")]
    )
    assert "本地等待已取消" in logs, "取消的是本插件等待：必须如实记录，不得静默或伪装成功"
    assert "撤回" not in logs and "撤销" not in logs, "不得宣称已撤回或撤销宿主侧的判断"


def test_已提交判断后的工具调用在源发送前撤销权限时零发送(tmp_path: Path) -> None:
    """5.7：从已提交判断继续发起工具调用；源发送前撤销权限即零发送并释放占位。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    message_id = "m-judgement-source"
    raw_message = [forward_segment(forward_node())]
    prepare_source(ctx, message_id=message_id, raw_message=raw_message)
    ctx.send.forward_results = [{"sent": True, "message_id": "m-should-not-send"}]
    arguments = issue_source_call(plugin, message_id=message_id)
    payload = received_message(message_id=message_id, raw_message=raw_message)

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        await plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(ctx.maisaka.proactive.calls))
        assert ctx.maisaka.proactive.calls[0]["metadata"]["source_message_id"] == message_id
        assert ctx.send.calls == [], "已提交的判断本身不发送消息"

        original_open_session = ctx.chat.open_session

        async def revoking_open_session(*args: Any, **kwargs: Any) -> Any:
            """预检期间（解析目标路由时）撤销目标白名单。"""

            await apply_config_update(plugin, enabled_config(target_groups=()))
            return await original_open_session(*args, **kwargs)

        ctx.chat.open_session = revoking_open_session
        result = await plugin.forward_message(**arguments)

        await plugin.on_unload()
        for _ in range(50):
            await asyncio.sleep(0)
        return result

    result = asyncio.run(scenario())

    assert result["status"] == TOOL_STATUS_REJECTED, result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == [], "源发送前撤销权限必须零发送"
    record = open_test_ledger(tmp_path).get(ledger_key(message_id=message_id))
    assert record is not None and record.status == STATUS_FAILED, (
        "撤销发生在发送前：占位必须按「明确未发送」释放（可显式重试）",
        record,
    )
    assert record.target_message_id == "" and record.comment_status == "", record
    assert not any("未退出" in text for text in ctx.logger.texts("warning")), (
        "卸载必须保持既有有界等待：本用例的任务都在上限内退出，不得留下孤立任务",
        ctx.logger.records,
    )
    assert "已卸载" in "\n".join(ctx.logger.texts("info")), ctx.logger.records


def test_已提交判断后的工具调用在评论等待期间撤销权限时保留源投递(tmp_path: Path) -> None:
    """5.7：源投递成功后撤销目标权限：不发评论、保留源成功记录、结果为部分成功。"""

    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    message_id = "m-judgement-comment"
    raw_message = [forward_segment(forward_node())]
    prepare_source(ctx, message_id=message_id, raw_message=raw_message)
    ctx.send.forward_results = [{"sent": True, "message_id": "m-target-src"}]
    ctx.send.text_results = [{"sent": True, "message_id": "m-target-comment"}]
    arguments = issue_source_call(plugin, message_id=message_id, comment="撤销后不得发送")
    payload = received_message(message_id=message_id, raw_message=raw_message)

    async def scenario() -> dict[str, Any]:
        await plugin.on_load()
        await plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(ctx.maisaka.proactive.calls))
        assert ctx.send.calls == [], "已提交的判断本身不发送消息"

        task = asyncio.create_task(plugin.forward_message(**arguments))
        await wait_until(lambda: clock.sleeps == [0.5])
        await apply_config_update(plugin, enabled_config(target_groups=()))
        clock.advance(0.5)
        return await task

    result = asyncio.run(scenario())

    assert result["status"] == "partial", result
    assert result["sent"] is True and result["target_message_id"] == "m-target-src", result
    assert result["comment_status"] == STATUS_FAILED, result
    assert [call["capability"] for call in ctx.send.calls] == ["send.forward"], (
        "评论等待期间撤销目标权限：不得发送评论",
        ctx.send.calls,
    )
    record = open_test_ledger(tmp_path).get(ledger_key(message_id=message_id))
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "m-target-src"), record
    assert record.comment_status == STATUS_FAILED, record
