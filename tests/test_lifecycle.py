"""公开生命周期与可信 Hook：仅管理本插件工作，等待后复验与共享卸载预算。

配置按宿主先注入后回调；断线、卸载和地址变化不能续发旧评论，已提交结果保守保留。
保留自动候选及标准的既有边界，并通过固定动作和账本公开事实验证竞态。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import plugin as plugin_module
from delivery_ledger import STATUS_SENT

from tests.direct_support import (make_direct_plugin, config_with, onebot_segments, store_source_message,
                                  delivery_rows, comment_sleeps, DirectOneBot)
from tests.support import (
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    FakeClock,
    apply_config_update,
    at_segment,
    enabled_config,
    forward_node,
    forward_segment,
    intent_criteria_region,
    issue_source_call,
    make_plugin,
    merged_forward_message,
    prepare_source,
    received_message,
    run_bound_forward,
    source_stream,
    text_segment,
    wait_until,
)


def _plugin_sleeps(clock: FakeClock) -> list[float]:
    """评论/自动判断的半秒等待；过滤独立的动作超时看门狗。"""

    return comment_sleeps(clock)


# --------------------------------------------------------------------------- 7.1


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
    """卸载取消评论等待中的投递：不提交评论授权、不发送评论，源已投递事实保留。"""

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])

        await fixture.plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.done(), "卸载必须取消并等待在途投递任务退出（不得让它在后台继续）"
        assert task.done() and (await task)["status"] == "partial", "取消评论等待仍报告已知源事实"
        assert not any("未退出" in text for text in fixture.ctx.logger.texts("warning")), (
            "被取消的投递任务在卸载等待上限内退出，不得留下「未退出」告警",
            fixture.ctx.logger.records,
        )
        assert "已卸载" in "\n".join(fixture.ctx.logger.texts("info")), (
            "卸载必须等待被取消的投递任务退出并留下完成记录",
            fixture.ctx.logger.records,
        )
        assert sum(call["stage"] == "comment" for call in fixture.onebot.sent) == 0, "卸载后不得提交评论授权"

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None
    assert record["status"] == STATUS_SENT, "已成功发送的源消息事实不得因卸载回滚"
    assert fixture.onebot.send_count == 1, "卸载后不得再发送评论"
    fixture.close()


def test_卸载等待超时告警可定位且不再发起新的发送(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    entered, release = asyncio.Event(), asyncio.Event()
    async def uncooperative(*args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return None
    ctx.message.get_by_id = uncooperative
    async def scenario():
        await plugin.observe_received_message(**merged_forward_message(message_id="m-stuck"))
        await entered.wait()
        unloading = asyncio.create_task(plugin.on_unload())
        for _ in range(30):
            await asyncio.sleep(0)
        clock.advance(5)
        try:
            await wait_until(unloading.done)
            await unloading
            warnings = "\n".join(ctx.logger.texts("warning"))
            assert "未退出" in warnings and "m-stuck" in warnings and "自动判断" in warnings
            assert ctx.send.calls == [] and ctx.maisaka.proactive.calls == []
        finally:
            release.set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert ctx.maisaka.proactive.calls == []
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

    assert result["status"] == "rejected", result
    assert "已卸载" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert ctx.send.calls == []
    assert ctx.chat.calls == [], "卸载后不得再查询宿主会话"


def test_预检期间卸载时不再创建发送任务(tmp_path: Path) -> None:
    """预检期间发生卸载：复检必须挡在创建发送任务与提交阶段授权之前。"""

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    original_get_by_id = fixture.ctx.message.get_by_id

    async def unloading_get_by_id(*args: Any, **kwargs: Any) -> Any:
        """预检期间（读取源消息时）插件被卸载；预检本身仍会照常结束。"""

        await fixture.plugin.on_unload()
        return await original_get_by_id(*args, **kwargs)

    fixture.ctx.message.get_by_id = unloading_get_by_id

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        return await fixture.plugin.forward_message(**arguments)

    result = asyncio.run(scenario())

    assert result["status"] == "rejected", result
    assert "已卸载" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0
    assert fixture.onebot.count("get_group_list") == 0, "卸载后不得再提交服务准备"
    assert delivery_rows(fixture.ledger) == [], (
        "卸载后不得留下任何投递占位",
    )
    fixture.close()


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

    fixture = make_direct_plugin(tmp_path)
    prepare_source(fixture.ctx, message_id="m-broadcast-1")
    fixture.ctx.message.sequences = {"m-broadcast-1": [None, None, None]}
    payload = merged_forward_message(
        message_id="m-broadcast-1",
        session_id=SOURCE_SESSION_ID,
        group_id=SOURCE_GROUP_ID,
    )

    async def scenario() -> None:
        await fixture.plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(_plugin_sleeps(fixture.clock)))

        await apply_config_update(
            fixture.plugin, config_with(fixture.plugin, auto={"enabled": False})
        )

        fixture.clock.advance(60.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert fixture.ctx.maisaka.proactive.calls == [], "自动入口关闭后不得再自动唤起"
        assert len(fixture.ctx.message.calls) == 1, "待执行唤起必须被取消，不得继续轮询宿主"

    asyncio.run(scenario())

    fixture.ctx.message.sequences = {}
    prepare_source(fixture.ctx)
    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", clock=fixture.clock)

    assert result["status"] == STATUS_SENT, result
    assert fixture.onebot.send_count == 1, (
        "自动入口关闭只停止自动唤起，不禁用总开关开启时的合法工具调用",
        fixture.onebot.send_count,
    )
    assert fixture.old_send_calls() == []
    fixture.close()


# --------------------------------------------------------------------------- 7.3


def test_源发送前权限撤销时零发送并释放占位(tmp_path: Path) -> None:
    """预检通过后、实际源发送前撤销权限：一次发送都不能发生，未消费授权被撤销。"""

    cases: list[tuple[str, dict[str, Any]]] = [
        ("撤销目标白名单", {"scope": {"target_groups": []}}),
        ("撤销源白名单", {"scope": {"source_groups": []}}),
        ("关闭总开关", {"plugin": {"enabled": False}}),
    ]
    for index, (label, revoked) in enumerate(cases):
        fixture = make_direct_plugin(tmp_path / f"revoked-{index}")
        original_get_by_id = fixture.ctx.message.get_by_id

        async def revoking_get_by_id(*args: Any, **kwargs: Any) -> Any:
            """预检期间（读取源消息时）发生配置热更新。"""

            await apply_config_update(fixture.plugin, config_with(fixture.plugin, **revoked))
            return await original_get_by_id(*args, **kwargs)

        fixture.ctx.message.get_by_id = revoking_get_by_id

        result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", clock=fixture.clock)

        assert fixture.onebot.send_count == 0, (
            label,
            "预检后撤销权限时不得进入源发送",
            fixture.onebot.send_count,
        )
        assert fixture.onebot.send_count == 0, (label, "不得提交源阶段授权")
        assert result["status"] == "rejected", (label, result)
        assert "本次未发送任何消息" in str(result["reason"]), (label, result)
        assert delivery_rows(fixture.ledger) == [], (
            label,
            "未发送前撤销不得留下任何投递事实",
        )
        assert fixture.onebot.calls == [], "本地预检失效不访问平台"
        fixture.close()


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

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])
        await fixture.plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.done() and (await task)["status"] == "partial", "取消评论等待仍报告已知源事实"
        assert not any("未退出" in text for text in fixture.ctx.logger.texts("warning")), (
            "评论等待中的投递在卸载等待上限内退出，不得留下「未退出」告警",
            fixture.ctx.logger.records,
        )
        assert "已卸载" in "\n".join(fixture.ctx.logger.texts("info")), (
            "卸载必须等待被取消的投递任务退出并留下完成记录",
            fixture.ctx.logger.records,
        )
        fixture.clock.advance(1.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert sum(call["stage"] == "comment" for call in fixture.onebot.sent) == 0, "卸载后不得提交评论授权"

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None
    assert record["status"] == STATUS_SENT, (
        "源消息已成功发送：事实与真实目标消息 ID 必须保留",
        record,
    )
    assert record["target_message_id"], record
    assert record["comment_status"] == "", (
        "被取消的评论不得留下任何已发送结果",
        record,
    )
    assert fixture.onebot.send_count == 1, "卸载后不得发送评论"

    # 重新加载插件实例后重复调用：不重发源消息，也不自动补发评论
    fixture.reopen()
    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", comment="不补发")
    assert repeat["status"] == "already_forwarded", repeat
    assert fixture.onebot.calls == [], "重复调用不得重发源消息或补发评论"
    fixture.close()


def test_源发送进行中卸载时保持未知且重载不重发(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin)
    fixture.onebot.gates.pause("platform_commit")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: fixture.onebot.send_count == 1)
        await fixture.plugin.on_unload()
        result = await task
        assert result["status"] == "unknown" and not result["success"]
        fixture.onebot.gates.release("platform_commit")
    asyncio.run(scenario())
    assert fixture.onebot.send_count == 1
    assert fixture.record()["status"] == "unknown"
    fixture.reopen()
    result, _ = run_bound_forward(fixture.plugin)
    assert result["status"] == "unknown" and fixture.onebot.calls == []
    fixture.close()


def test_评论等待中撤销目标权限时不发送评论并保留源成功记录(tmp_path: Path) -> None:
    """热更新撤销目标权限：评论不发送，源成功记录与目标消息 ID 保留，结果为部分成功。"""

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间被撤销")

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])
        await apply_config_update(
            fixture.plugin, config_with(fixture.plugin, scope={"target_groups": []})
        )
        fixture.clock.advance(0.5)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())

    assert result["status"] == "partial", result
    assert result["sent"] is True and result["target_message_id"], result
    assert result["comment_status"] == "failed", result
    assert "未发送" in str(result["reason"]), result
    assert fixture.onebot.send_count == 1, "撤销目标权限后不得发送评论"
    assert sum(call["stage"] == "comment" for call in fixture.onebot.sent) == 0, "撤销后不得提交评论授权"
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, record
    assert record["comment_status"] == "", record
    fixture.close()


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

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="热更新不取消投递")

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])

        await apply_config_update(
            fixture.plugin,
            config_with(
                fixture.plugin,
                auto={"message_types": ["text"], "share_criteria": "更新的自动分享标准"},
            ),
        )

        fixture.clock.advance(0.5)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())

    assert result["status"] == STATUS_SENT, result
    assert result["comment_status"] == "sent", result
    assert fixture.onebot.send_count == 2, (
        "热更新不得取消评论等待中的在途投递",
        fixture.onebot.send_count,
    )
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, record
    fixture.close()


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

    raw_message = [forward_segment(forward_node(content=[at_segment("all")]))]
    fixture = make_direct_plugin(tmp_path, raw_message=raw_message)
    message_id = "m-source-1"
    arguments = issue_source_call(fixture.plugin, message_id=message_id)
    payload = received_message(message_id=message_id, raw_message=raw_message)

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        await fixture.plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(fixture.ctx.maisaka.proactive.calls))
        assert fixture.ctx.maisaka.proactive.calls[0]["metadata"]["source_message_id"] == message_id
        assert fixture.onebot.send_count == 0, "已提交的判断本身不发送消息"

        original_get_by_id = fixture.ctx.message.get_by_id

        async def revoking_get_by_id(*args: Any, **kwargs: Any) -> Any:
            """预检期间（读取源消息时）撤销目标白名单。"""

            await apply_config_update(
                fixture.plugin, config_with(fixture.plugin, scope={"target_groups": []})
            )
            return await original_get_by_id(*args, **kwargs)

        fixture.ctx.message.get_by_id = revoking_get_by_id
        result = await fixture.plugin.forward_message(**arguments)

        await fixture.plugin.on_unload()
        for _ in range(50):
            await asyncio.sleep(0)
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "rejected", result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert fixture.onebot.send_count == 0, "源发送前撤销权限必须零发送"
    assert delivery_rows(fixture.ledger) == [], (
        "撤销发生在发送前：不得留下投递事实",
    )
    assert not any("未退出" in text for text in fixture.ctx.logger.texts("warning")), (
        "卸载必须保持既有有界等待：本用例的任务都在上限内退出，不得留下孤立任务",
        fixture.ctx.logger.records,
    )
    assert "已卸载" in "\n".join(fixture.ctx.logger.texts("info")), fixture.ctx.logger.records
    fixture.close()


def test_已提交判断后的工具调用在评论等待期间撤销权限时保留源投递(tmp_path: Path) -> None:
    """5.7：源投递成功后撤销目标权限：不发评论、保留源成功记录、结果为部分成功。"""

    raw_message = [text_segment("源消息正文")]
    fixture = make_direct_plugin(tmp_path, message_types=("text",), raw_message=raw_message)
    message_id = "m-source-1"
    arguments = issue_source_call(fixture.plugin, message_id=message_id, comment="撤销后不得发送")
    payload = received_message(message_id=message_id, raw_message=raw_message)

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        await fixture.plugin.observe_received_message(**payload)
        await wait_until(lambda: bool(fixture.ctx.maisaka.proactive.calls))
        assert fixture.onebot.send_count == 0, "已提交的判断本身不发送消息"

        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])
        await apply_config_update(
            fixture.plugin, config_with(fixture.plugin, scope={"target_groups": []})
        )
        fixture.clock.advance(0.5)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())

    assert result["status"] == "partial", result
    assert result["sent"] is True and result["target_message_id"], result
    assert result["comment_status"] == "failed", result
    assert fixture.onebot.send_count == 1, (
        "评论等待期间撤销目标权限：不得发送评论",
        fixture.onebot.send_count,
    )
    record = fixture.record(message_id=message_id)
    assert record is not None
    assert record["status"] == STATUS_SENT and record["target_message_id"], record
    assert record["comment_status"] == "", record
    fixture.close()

def test_卸载关闭失败不宣称全部退出也不提交评论(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin, comment="不能补发")
    async def broken_close():
        raise RuntimeError("合成关闭失败")
    fixture.onebot.close = broken_close
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])
        await fixture.plugin.on_unload()
        assert (await task)["status"] == "partial"
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        assert "关闭未确认" in warnings and "不宣称全部退出" in warnings
        assert fixture.onebot.send_count == 1
        assert fixture.record()["status"] == "sent"
    asyncio.run(scenario())
    fixture.ledger.close()


def test_配置签名变化时旧连接关闭且新连接独立(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    first, _ = run_bound_forward(fixture.plugin)
    assert first["status"] == "sent"
    old = fixture.onebot
    asyncio.run(apply_config_update(fixture.plugin, config_with(fixture.plugin,
        snowluma={"websocket_url": "ws://127.0.0.1:8766/onebot"})))
    fixture.onebot = DirectOneBot(fixture.ctx, (TARGET_GROUP_ID,))
    prepare_source(fixture.ctx, message_id="m-new")
    result, _ = run_bound_forward(fixture.plugin, message_id="m-new")
    assert result["status"] == "sent" and old.closed
    assert not fixture.onebot.closed and fixture.onebot.send_count == 1
    fixture.close()


def test_配置签名变化不中止已提交源但阻止旧评论(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    args = issue_source_call(fixture.plugin, comment="旧评论")
    fixture.onebot.gates.pause("platform_commit")
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await wait_until(lambda: fixture.onebot.send_count == 1)
        await apply_config_update(fixture.plugin, config_with(fixture.plugin,
            snowluma={"token_env_var": "CHANGED_TOKEN"}))
        assert not fixture.onebot.closed
        fixture.onebot.gates.release("platform_commit")
        await wait_until(lambda: 0.5 in fixture.clock.sleeps)
        fixture.clock.advance(0.5)
        result = await task
        assert result["status"] == "partial" and result["target_message_id"] == "target-1"
        assert fixture.onebot.send_count == 1 and fixture.record()["status"] == "sent"
    asyncio.run(scenario())
    fixture.close()


def test_多项未退出任务共用五秒预算且逐条告警(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
    entered = []
    release = asyncio.Event()
    async def stubborn(message_id, **kwargs):
        entered.append(message_id)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return None
    ctx.message.get_by_id = stubborn
    async def scenario():
        for message_id in ("m-wait-a", "m-wait-b"):
            await plugin.observe_received_message(**merged_forward_message(message_id=message_id))
        await wait_until(lambda: len(entered) == 2)
        unloading = asyncio.create_task(plugin.on_unload())
        for _ in range(30):
            await asyncio.sleep(0)
        clock.advance(5)
        try:
            await wait_until(unloading.done)
            await unloading
            warnings = "\n".join(ctx.logger.texts("warning"))
            assert "m-wait-a" in warnings and "m-wait-b" in warnings
            assert warnings.count("五秒预算内未退出") == 2
            assert ctx.send.calls == [] and ctx.maisaka.proactive.calls == []
        finally:
            release.set()
        for _ in range(50):
            await asyncio.sleep(0)
    asyncio.run(scenario())


def test_旧连接关闭等待期间卸载不新建投递也不重复关闭(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    assert run_bound_forward(fixture.plugin)[0]["status"] == "sent"
    old = fixture.onebot
    entered, release = asyncio.Event(), asyncio.Event()
    closes = []
    async def held_close():
        closes.append("close")
        entered.set()
        await release.wait()
        old.closed = True
    old.close = held_close
    fixture.onebot = DirectOneBot(fixture.ctx, (TARGET_GROUP_ID,))
    prepare_source(fixture.ctx, message_id="m-new")
    args = issue_source_call(fixture.plugin, message_id="m-new")
    async def scenario():
        await apply_config_update(fixture.plugin, config_with(fixture.plugin,
            snowluma={"websocket_url": "ws://127.0.0.1:8766/onebot"}))
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        await entered.wait()
        unloading = asyncio.create_task(fixture.plugin.on_unload())
        for _ in range(30):
            await asyncio.sleep(0)
        release.set()
        await unloading
        result = await task
        assert result["status"] == "rejected"
        assert closes == ["close"]
        assert fixture.onebot.calls == []
    asyncio.run(scenario())
    fixture.ledger.close()


@pytest.mark.parametrize("hold_resources", [False, True])
def test_默认工厂孤立动作卸载告警保留源目标阶段(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold_resources: bool) -> None:
    """真实客户端与传输；只替换网络库会话，Tool 已返回后仍能定位残留。"""
    import json
    from functools import partial
    from types import SimpleNamespace

    aiohttp = pytest.importorskip("aiohttp")
    from delivery_ledger import LEDGER_FILE_NAME, LedgerUnavailable, open_ledger
    from tests.snowluma_transport_support import CancellationGate, MemorySession, MemorySocket
    from tests.support import SOURCE_ACCOUNT_ID, function_call_item, open_test_ledger
    from maimai_courier_plugin.snowluma_transport import AiohttpOneBotTransport, OneBotConnection

    clock = FakeClock()
    write_gate, receive_gate, close_gate = CancellationGate(), CancellationGate(), CancellationGate()

    class SourceSocket(MemorySocket):
        async def send_json(self, body):
            self.sent.append(body)
            if body["action"] == "get_msg":
                await write_gate.wait()
                return
            data = ({"user_id": SOURCE_ACCOUNT_ID} if body["action"] == "get_login_info"
                    else [{"group_id": TARGET_GROUP_ID}])
            self.frames.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(
                {"status": "ok", "retcode": 0, "data": data, "echo": body["echo"]})))

        async def receive(self):
            try:
                return await super().receive()
            except asyncio.CancelledError:
                await receive_gate.wait()
                return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)

    socket = SourceSocket()
    session = MemorySession(socket)
    if hold_resources:
        async def held_socket_close():
            await close_gate.wait()
            await MemorySocket.close(socket)
        async def held_session_close():
            await close_gate.wait()
            await MemorySession.close(session)
        socket.close = held_socket_close
        session.close = held_session_close
    peer_socket = SourceSocket()
    peer = AiohttpOneBotTransport(OneBotConnection("ws://127.0.0.1:8765/onebot", "SYNTHETIC_TOKEN"),
        clock=clock, environ={"SYNTHETIC_TOKEN": "synthetic-only"}, session_factory=lambda **_: MemorySession(peer_socket))
    monkeypatch.setattr(plugin_module, "AiohttpOneBotTransport", partial(
        plugin_module.AiohttpOneBotTransport, session_factory=lambda **_: session))
    ledger = open_test_ledger(tmp_path, clock=clock)
    config = enabled_config()
    config["snowluma"] = {"websocket_url": "ws://127.0.0.1:8765/onebot", "token_env_var": "SYNTHETIC_TOKEN"}
    plugin, ctx = make_plugin(tmp_path, config=config, clock=clock,
        environ={"SYNTHETIC_TOKEN": "synthetic-only"}, ledger_opener=lambda _: ledger)
    prepare_source(ctx, message_id="m-orphan-a")
    arguments = issue_source_call(plugin, message_id="m-orphan-a")

    async def scenario():
        try:
            await plugin.on_load()
            assert (await peer.get_login_info())["data"]["user_id"] == SOURCE_ACCOUNT_ID
            tool = asyncio.create_task(plugin.forward_message(**arguments))
            await write_gate.entered.wait()
            clock.advance(10)
            assert (await tool)["status"] == "rejected"
            # 第一条工具及其外层投递已结束，再制造另一条排队工作，不能串来源。
            prepare_source(ctx, message_id="m-orphan-b")
            bound = await plugin.bind_forward_source(session_id=SOURCE_SESSION_ID, output_items=[
                function_call_item(func_name="forward_message", arguments={
                    "message_id": "m-orphan-b", "target_group_id": TARGET_GROUP_ID})])
            second_args = bound["modified_kwargs"]["output_items"][0]["tool_call"]["args"]
            previous_sleeps = len(clock.sleeps)
            second = asyncio.create_task(plugin.forward_message(**second_args))
            await wait_until(lambda: len(clock.sleeps) > previous_sleeps)
            clock.advance(10)
            assert (await second)["status"] == "rejected"
            unload = asyncio.create_task(plugin.on_unload())
            await receive_gate.entered.wait()
            await wait_until(lambda: 5.0 in clock.sleeps)
            clock.advance(5)
            await unload
            warnings = [text for text in ctx.logger.texts("warning") if "五秒预算内未退出" in text]
            action_warning = next(text for text in warnings if "SnowLuma get_msg" in text and "直连关闭" not in text)
            assert "源消息=m-orphan-a" in action_warning and "目标群=20002" in action_warning and "阶段=源准备" in action_warning
            assert "m-orphan-b" not in action_warning
            receiver_warning = next(text for text in warnings if "SnowLuma 接收" in text and "直连关闭" not in text)
            assert "共享连接" in receiver_warning and "阶段=" in receiver_warning
            assert "m-orphan-a" not in receiver_warning and "m-orphan-b" not in receiver_warning
            if hold_resources:
                for name in ("SnowLuma 关闭连接", "SnowLuma 关闭会话"):
                    warning = next(text for text in warnings if name in text and "直连关闭" not in text)
                    assert "共享连接" in warning and "阶段=连接管理" in warning
            assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME, clock=clock), LedgerUnavailable)
            assert (await peer.get_login_info())["data"]["user_id"] == SOURCE_ACCOUNT_ID
            assert not peer_socket.closed
            assert all(body["action"] != "send_msg" for body in socket.sent)
            assert ctx.send.calls == []
        finally:
            write_gate.release.set()
            receive_gate.release.set()
            close_gate.release.set()
            await peer.close()
            for _ in range(100):
                await asyncio.sleep(0)

    try:
        asyncio.run(scenario())
    finally:
        ledger.close()


@pytest.mark.parametrize("remaining_jump", [1.0, 2.0])
@pytest.mark.parametrize("atomic_transport", [False, True])
def test_阻塞账本关闭纳入卸载剩余预算且保留真实使用权(tmp_path: Path, remaining_jump: float, atomic_transport: bool) -> None:
    """网络关闭四秒后存储仍阻塞；事件循环独立推进最后一秒，不取消底层工作。"""
    import threading
    from delivery_ledger import LEDGER_FILE_NAME, LedgerUnavailable, open_ledger

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin)
    release = threading.Event()
    finished = threading.Event()
    close_calls = []
    transport_calls = []

    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def blocking_close():
            close_calls.append("close")
            loop.call_soon_threadsafe(entered.set)
            # 仅为红灯实现阻塞事件循环时兜底退出；绿灯必须在释放前返回卸载。
            release.wait(2)
            try:
                fixture.ledger.close()
            finally:
                finished.set()

        async def delayed_transport_close():
            transport_calls.append("close")
            if atomic_transport:
                fixture.clock.advance(4)
            else:
                await fixture.clock.sleep(4)
            fixture.onebot.closed = True

        fixture.storage.close = blocking_close
        fixture.onebot.close = delayed_transport_close
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"
        unload = asyncio.create_task(fixture.plugin.on_unload())
        try:
            if not atomic_transport:
                await wait_until(lambda: 4.0 in fixture.clock.sleeps)
                fixture.clock.advance(4)
            await entered.wait()
            assert not finished.is_set(), "同步账本关闭必须在事件循环之外运行"
            fixture.clock.advance(remaining_jump)
            await wait_until(unload.done)
            await unload
            assert fixture.clock.now() == 4 + remaining_jump
            assert not finished.is_set(), "五秒到期不能伪称底层存储工作已结束"
            warnings = "\n".join(fixture.ctx.logger.texts("warning"))
            assert "账本关闭" in warnings and "阶段=存储关闭" in warnings and "未确认" in warnings
            assert "不宣称全部退出" in warnings
            assert not any("已卸载" in text for text in fixture.ctx.logger.texts("info"))
            assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable)
            await fixture.plugin.on_unload()
            assert close_calls == ["close"] and transport_calls == ["close"]
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 2)
            await unload
        reopened = open_ledger(tmp_path / LEDGER_FILE_NAME)
        assert not isinstance(reopened, LedgerUnavailable), "真实关闭完成后才允许新实例取得使用权"
        await fixture.plugin.on_unload()
        assert close_calls == ["close"] and transport_calls == ["close"]
        assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable), "重复卸载不能关闭新实例使用的文件描述符"
        reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_账本快速关闭与失败均只执行一次且日志安全(tmp_path: Path, failure: bool) -> None:
    from delivery_ledger import LEDGER_FILE_NAME, LedgerUnavailable, open_ledger

    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin)
    close_calls = []
    sensitive = "synthetic-token-and-message-body"

    def close_storage():
        close_calls.append("close")
        if failure:
            raise RuntimeError(sensitive)
        fixture.ledger.close()

    fixture.storage.close = close_storage

    async def scenario():
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"
        await fixture.plugin.on_unload()
        await fixture.plugin.on_unload()
        assert close_calls == ["close"]
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        info = "\n".join(fixture.ctx.logger.texts("info"))
        assert sensitive not in warnings + info
        if failure:
            assert "账本关闭" in warnings and "未确认" in warnings and "不宣称全部退出" in warnings
            assert "已卸载" not in info
            assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable)
        else:
            assert "已卸载" in info and warnings == ""
            reopened = open_ledger(tmp_path / LEDGER_FILE_NAME)
            assert not isinstance(reopened, LedgerUnavailable)
            reopened.close()

    try:
        asyncio.run(scenario())
    finally:
        fixture.ledger.close()


def test_同步账本关闭完成但原子跨过期限仍报告超时(tmp_path: Path) -> None:
    fixture = make_direct_plugin(tmp_path)
    arguments = issue_source_call(fixture.plugin)

    async def scenario():
        loop = asyncio.get_running_loop()

        async def delayed_transport_close():
            await fixture.clock.sleep(4)
            fixture.onebot.closed = True

        def close_storage():
            fixture.ledger.close()
            # 在关闭结果返回事件循环之前，原子推进到总计六秒。
            loop.call_soon_threadsafe(fixture.clock.advance, 2)

        fixture.storage.close = close_storage
        fixture.onebot.close = delayed_transport_close
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"
        unload = asyncio.create_task(fixture.plugin.on_unload())
        await wait_until(lambda: 4.0 in fixture.clock.sleeps)
        fixture.clock.advance(4)
        await unload
        assert fixture.clock.now() == 6
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        assert "超出五秒总预算" in warnings and "不宣称全部退出" in warnings
        assert not any("已卸载" in text for text in fixture.ctx.logger.texts("info"))

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["源准备", "源账号查询", "源排队", "评论等待", "评论账号查询", "评论排队"])
@pytest.mark.parametrize("change", ["总开关", "来源白名单", "目标白名单", "地址", "凭证引用", "配置令牌", "卸载", "断线"])
def test_异步等待后按最新权限和连接条件阻止提交(tmp_path: Path, stage: str, change: str) -> None:
    fixture = make_direct_plugin(tmp_path)
    if change == "配置令牌":
        fixture.plugin.set_plugin_config(config_with(fixture.plugin,
            snowluma={"token": "synthetic-original-token"}))
    args = issue_source_call(fixture.plugin, comment="本次评论")
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_query(name, value):
        wanted = (stage == "源准备" and name == "get_group_list"
                  or stage == "源账号查询" and name == "get_login_info" and fixture.onebot.count(name) == 2
                  or stage == "评论账号查询" and name == "get_login_info" and fixture.onebot.count(name) == 3)
        if wanted:
            entered.set()
            await release.wait()
    async def hold_wire(which):
        if (stage == "源排队" and which == "source") or (stage == "评论排队" and which == "comment"):
            entered.set()
            await release.wait()
    fixture.onebot.on_action = hold_query
    fixture.onebot.before_wire = hold_wire
    async def scenario():
        task = asyncio.create_task(fixture.plugin.forward_message(**args))
        if stage.startswith("评论"):
            await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])
            if stage != "评论等待":
                fixture.clock.advance(0.5)
                await entered.wait()
        else:
            await entered.wait()
        if change == "卸载":
            await fixture.plugin.on_unload()
        elif change == "断线":
            fixture.onebot.connection_generation += 1
        else:
            override = {"总开关": {"plugin": {"enabled": False}},
                        "来源白名单": {"scope": {"source_groups": []}},
                        "目标白名单": {"scope": {"target_groups": []}},
                        "地址": {"snowluma": {"websocket_url": "ws://127.0.0.1:8766/onebot"}},
                        "凭证引用": {"snowluma": {"token_env_var": "CHANGED_TOKEN"}},
                        "配置令牌": {"snowluma": {"token": "synthetic-changed-token"}}}[change]
            await apply_config_update(fixture.plugin, config_with(fixture.plugin, **override))
        release.set()
        if stage == "评论等待":
            fixture.clock.advance(0.5)
        result = await task
        if stage.startswith("评论"):
            assert result["status"] == "partial" and result["target_message_id"] == "target-1"
            assert fixture.onebot.send_count == 1
            assert fixture.record()["status"] == "sent"
        else:
            # 断线属于传输明确未提交，其余许可失效必须明确 rejected。
            assert result["status"] in (("rejected", "failed") if change == "断线" else ("rejected",))
            assert fixture.onebot.send_count == 0
            record = fixture.record()
            assert record is None or record["status"] == "failed"
    asyncio.run(scenario())
    fixture.close()
