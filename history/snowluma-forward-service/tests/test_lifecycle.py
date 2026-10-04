"""第 7 组迁移版：生命周期、取消与热更新（7.1–7.4）。

迁移口径（本变更第 6 组）：发送链已迁到独立服务。本文件的契约不变——插件持有并
等待自己创建的**全部**后台任务；卸载或关闭总开关时立即停止签发新的阶段授权并取消
尚未提交的本地工作；已交付给服务端的阶段调用按真实结果保留（客户端取消不撤销
服务端已开始的平台 RPC）；等待有上限、超时告警可定位；白名单热更新在源/评论阶段
授权前复验；自动入口开关只停自动唤起，不禁用工具；不操作其他插件的任务。

用例只经 SDK 生命周期入口（`on_load` / `on_unload` / `on_config_update`）、观察 Hook
输入事件与 `forward_message` 公共入口驱动；服务端事实经只读查询或独立连接观察。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import plugin as plugin_module
from delivery_ledger import STATUS_SENT

from tests.client_support import (
    ClientServiceFixture,
    ServiceAppTransport,
    config_with,
    make_client_service,
    onebot_segments,
)
from tests.service_support import state_ledger_rows, store_source_message
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
    """插件侧登记的等待（服务端时钟独立，不污染该列表）。"""

    return list(clock.sleeps)


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

    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])

        await fixture.plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.done(), "卸载必须取消并等待在途投递任务退出（不得让它在后台继续）"
        assert task.cancelled(), "被卸载取消的投递任务应如实表现为取消"
        assert not any("未退出" in text for text in fixture.ctx.logger.texts("warning")), (
            "被取消的投递任务在卸载等待上限内退出，不得留下「未退出」告警",
            fixture.ctx.logger.records,
        )
        assert "已卸载" in "\n".join(fixture.ctx.logger.texts("info")), (
            "卸载必须等待被取消的投递任务退出并留下完成记录",
            fixture.ctx.logger.records,
        )
        assert fixture.transport.count("/comment") == 0, "卸载后不得提交评论授权"

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None
    assert record["status"] == STATUS_SENT, "已成功发送的源消息事实不得因卸载回滚"
    assert fixture.onebot.send_count == 1, "卸载后不得再发送评论"
    fixture.close()


def test_卸载等待超时告警可定位且不再发起新的发送(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主/服务调用吞掉取消时：卸载按上限返回、超时告警可定位，其后零新发送。

    上限在这里被缩短到 0.05 秒：本用例验证的是「等待有界 + 告警不静默 + 告警可定位」
    这一分支，而不是 5 秒这个字面值；不缩短就要真实等待 5 秒。
    """

    monkeypatch.setattr(plugin_module, "TASK_EXIT_WAIT_SECONDS", 0.05)
    fixture = make_client_service(tmp_path, request_timeout_seconds=60.0)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="卸载后不得补发")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_request = fixture.transport.request

    async def stubborn_request(method: str, url: str, **kwargs: Any) -> Any:
        """源阶段请求吞掉一次取消后继续等待，释放后才按真实结果返回。"""

        if url.endswith("/source") and not entered.is_set():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        return await original_request(method, url, **kwargs)

    fixture.transport.request = stubborn_request  # type: ignore[method-assign]

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await entered.wait()

        await fixture.plugin.on_unload()

        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        assert "未退出" in warnings, (
            "等待上限内没退出的任务必须告警，不得静默留下孤立任务",
            fixture.ctx.logger.records,
        )
        assert "m-source-1" in warnings and TARGET_GROUP_ID in warnings, (
            "超时告警必须能定位源消息与目标群",
            fixture.ctx.logger.records,
        )
        assert "已卸载" in "\n".join(fixture.ctx.logger.texts("info")), (
            "等待超时后卸载仍必须返回并留下完成记录",
            fixture.ctx.logger.records,
        )
        assert not task.done(), "吞掉取消的在途发送仍在跑（服务调用可能仍完成）"

        release.set()
        for _ in range(200):
            if state_ledger_rows(fixture.service.state_dir, message_id="m-source-1"):
                break
            await asyncio.sleep(0)
        assert fixture.transport.count("/source") == 1, (
            "卸载返回后只允许已经在途的源发送完成，不得再发起任何新的发送（不补发评论）",
            fixture.transport.paths(),
        )
        assert fixture.transport.count("/comment") == 0, "卸载后不得补发评论"
        if not task.done():
            task.cancel()
        return await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    # 注销先完成：迟到的阶段请求不能重新激活已撤销的未消费授权。
    assert fixture.onebot.send_count == 0, (
        "卸载/注销先完成时，迟到的旧请求不得启动任何平台发送",
        fixture.onebot.send_count,
    )
    assert state_ledger_rows(fixture.service.state_dir) == [], (
        "撤销先完成时不得留下任何投递事实",
    )
    fixture.close()


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

    fixture = make_client_service(tmp_path)
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
    assert fixture.transport.count("/prepare") == 0, "卸载后不得再提交服务准备"
    assert state_ledger_rows(fixture.service.state_dir) == [], (
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

    fixture = make_client_service(tmp_path)
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
        fixture = make_client_service(tmp_path / f"revoked-{index}")
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
        assert fixture.transport.count("/source") == 0, (label, "不得提交源阶段授权")
        assert result["status"] == "rejected", (label, result)
        assert "本次未发送任何消息" in str(result["reason"]), (label, result)
        assert state_ledger_rows(fixture.service.state_dir) == [], (
            label,
            "未发送前撤销不得留下任何投递事实",
        )
        assert fixture.transport.count("/cancel") <= 1, (
            label,
            "最多请求一次远端撤销；未消费授权不得被重复提交",
        )
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

    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])
        await fixture.plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.cancelled(), "卸载必须取消评论等待中的投递"
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
        assert fixture.transport.count("/comment") == 0, "卸载后不得提交评论授权"

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None
    assert record["status"] == STATUS_SENT, (
        "源消息已成功发送：事实与真实目标消息 ID 必须保留",
        record,
    )
    assert record["target_message_id"], record
    assert record["comment_status"] in ("not_requested", "pending", "skipped"), (
        "被取消的评论不得留下任何已发送结果",
        record,
    )
    assert fixture.onebot.send_count == 1, "卸载后不得发送评论"

    # 重新加载插件实例后重复调用：不重发源消息，也不自动补发评论
    again = fixture.fresh_plugin()
    repeat, _ = run_bound_forward(again.plugin, message_id="m-source-1", comment="不补发")
    assert repeat["status"] == "already_forwarded", repeat
    assert fixture.onebot.send_count == 1, "重复调用不得重发源消息或补发评论"
    fixture.close()


def test_源发送进行中卸载时保留服务端真实结果(tmp_path: Path) -> None:
    """源发送进行中卸载/取消：服务端已开始的平台 RPC 不受客户端取消影响。

    这是 design D9 的公开语义：客户端断开不取消服务端已经开始的平台调用，服务端
    仍尝试完成落账；客户端不得把这种结果误判为「明确未发送」。
    """

    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    fixture.onebot.gates.pause("platform_commit")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.onebot.gates.arrivals.get("platform_commit", 0) == 1)

        await fixture.plugin.on_unload()
        for _ in range(50):
            await asyncio.sleep(0)
        assert task.cancelled(), "卸载必须取消在途源发送的本地等待"

        # 服务端已开始的平台调用继续完成：放行后按真实回执落账。
        fixture.onebot.gates.release("platform_commit")
        await wait_until(
            lambda: [row[1] for row in state_ledger_rows(fixture.service.state_dir, message_id="m-source-1")]
            == [STATUS_SENT]
        )

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, (
        "客户端卸载不得把服务端已完成的平台调用误判为未发送",
        record,
    )
    assert record["target_message_id"], record

    # 重新加载插件实例后重复调用：既有成功事实阻止重发，不得降为可安全重试。
    again = fixture.fresh_plugin()
    sends_before = fixture.onebot.send_count
    repeat, _ = run_bound_forward(again.plugin, message_id="m-source-1")
    assert repeat["status"] == "already_forwarded", repeat
    assert fixture.onebot.send_count == sends_before, "真实结果必须阻止重复发送"
    fixture.close()


def test_评论等待中撤销目标权限时不发送评论并保留源成功记录(tmp_path: Path) -> None:
    """热更新撤销目标权限：评论不发送，源成功记录与目标消息 ID 保留，结果为部分成功。"""

    fixture = make_client_service(tmp_path)
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
    assert fixture.transport.count("/comment") == 0, "撤销后不得提交评论授权"
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, record
    assert record["comment_status"] in ("failed", "skipped"), record
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

    fixture = make_client_service(tmp_path)
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
    fixture = make_client_service(tmp_path, raw_message=raw_message)
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
    assert state_ledger_rows(fixture.service.state_dir) == [], (
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
    fixture = make_client_service(tmp_path, message_types=("text",), raw_message=raw_message)
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
    assert record["comment_status"] in ("failed", "skipped"), record
    fixture.close()

def test_卸载时远端撤销不可达不宣称已停止(tmp_path: Path) -> None:
    """D6：本地先停止新授权，再请求远端撤销；撤销不可达时如实报告，不伪称已停止。"""

    fixture = make_client_service(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: _plugin_sleeps(fixture.clock) == [0.5])

        # 撤销与注销都不可达：卸载必须仍然返回，并如实报告未获确认。
        fixture.transport.fail_paths.append("/cancel")
        fixture.transport.fail_paths.append(f"/v1/sessions/{fixture.plugin._service.session_id}")  # noqa: SLF001
        await fixture.plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.cancelled(), "卸载必须立即停止本地等待并取消投递任务"

    asyncio.run(scenario())

    warnings = "\n".join(fixture.ctx.logger.texts("warning"))
    assert "未获确认" in warnings, (
        "撤销不可达必须如实告警，不得宣称远端已停止",
        fixture.ctx.logger.records,
    )
    assert "不宣称远端已停止" in warnings, (
        "告警必须明确声明没有远端已停止的证据",
        fixture.ctx.logger.records,
    )
    assert fixture.transport.count("/comment") == 0, "本地已停止新授权，评论不得提交"
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, record
    fixture.close()


# -------------- 本变更第 6 组复核修复：退役会话关闭与撤销总时限（6.8）

class _GatedSourceTransport:
    """在源阶段请求处挂起的传输替身：验证退役会话不中止在途投递。

    第 6 组复核修复前 `plugin._service_ready` 只把旧会话从 `self._service` 摘除，
    不关闭其传输；本替身用于观察「在途任务仍持有旧会话」与「随后关闭」两个时点。
    """

    def __init__(self, inner: ServiceAppTransport) -> None:
        self.inner = inner
        self.source_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Any,
        body: bytes,
    ) -> Any:
        if url.endswith("/source"):
            self.source_entered.set()
            await self.release.wait()
        return await self.inner.request(method, url, headers=headers, body=body)

    async def close(self) -> None:
        self.closed = True
        await self.inner.close()


def _recording_transports(fixture: ClientServiceFixture) -> tuple[list[Any], Any]:
    """逐会话新建内存传输的记录工厂：每个 `RemoteServiceSession` 一个独立传输。"""

    transports: list[Any] = []

    def factory(section: Any) -> Any:
        transport = ServiceAppTransport(fixture.service.app)
        transports.append(transport)
        return transport

    return transports, factory


def test_配置签名变化时退役会话的传输被关闭(tmp_path: Path) -> None:
    """6.8 复核修复：服务地址/数据集变化后旧会话的传输必须显式关闭，不得泄漏连接池。"""

    fixture = make_client_service(tmp_path)
    transports, factory = _recording_transports(fixture)
    plugin = fixture.fresh_plugin(transport_factory=factory).plugin
    await_first = issue_source_call(plugin, message_id="m-source-1")
    await_second = issue_source_call(plugin, message_id="m-source-1")

    async def scenario() -> None:
        await plugin.on_load()
        first = await plugin.forward_message(**await_first)
        assert first["status"] == "sent", first
        assert len(transports) == 1 and transports[0].closed is False, "首次调用建立一个会话"

        # 换服务地址与客户端身份：配置签名变化触发会话替换（新会话随后因身份不符被
        # 服务端拒绝也不影响本用例的观察点——替换发生在任何会话请求之前）。
        await apply_config_update(
            plugin,
            config_with(
                plugin,
                service={"endpoint": "http://127.0.0.1:8791", "client_identity": "maibot-plugin-2"},
            ),
        )
        await plugin.forward_message(**await_second)

        for _ in range(100):
            if transports[0].closed:
                break
            await asyncio.sleep(0)
        assert len(transports) == 2, ("替换必须建立新会话的传输", len(transports))
        assert transports[0].closed is True, "退役会话的传输必须被关闭（不得泄漏 aiohttp 会话）"
        assert transports[1].closed is False, "新会话的传输不得被误关"

    asyncio.run(scenario())
    fixture.close()


def test_配置签名变化不中止在途投递且在其完成后关闭旧传输(tmp_path: Path) -> None:
    """6.8 复核修复：先摘除旧会话、等在途任务退出、再关闭；在途投递不得被误报中止。"""

    fixture = make_client_service(tmp_path)
    store_source_message(
        fixture.onebot,
        message_id="m-source-2",
        group_id=SOURCE_GROUP_ID,
        message=onebot_segments([{"type": "text", "data": "另一条源消息"}]),
    )
    transports: list[ServiceAppTransport] = []
    gated: list[_GatedSourceTransport] = []

    def factory(section: Any) -> Any:
        inner = ServiceAppTransport(fixture.service.app)
        transports.append(inner)
        if len(transports) == 1:
            wrapper = _GatedSourceTransport(inner)
            gated.append(wrapper)
            return wrapper
        return inner

    plugin = fixture.fresh_plugin(transport_factory=factory).plugin
    first = issue_source_call(plugin, message_id="m-source-1")
    second = issue_source_call(plugin, message_id="m-source-2")

    async def scenario() -> None:
        await plugin.on_load()
        task = asyncio.create_task(plugin.forward_message(**first))
        await wait_until(lambda: len(gated) == 1)
        await asyncio.wait_for(gated[0].source_entered.wait(), timeout=5.0)

        await apply_config_update(
            plugin,
            config_with(
                plugin,
                service={"endpoint": "http://127.0.0.1:8791", "client_identity": "maibot-plugin-2"},
            ),
        )
        # 第二次调用触发会话替换；新会话因身份不符被拒，不影响旧会话的未消费授权。
        await plugin.forward_message(**second)
        assert len(transports) == 2, transports
        assert transports[0].closed is False, "在途投递仍持有旧会话时不得关闭其传输"

        gated[0].release.set()
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result["status"] == "sent", ("退役不得中止或误报在途投递", result)

        for _ in range(100):
            if transports[0].closed:
                break
            await asyncio.sleep(0)
        assert transports[0].closed is True, "在途投递完成后旧会话的传输必须关闭"

    asyncio.run(scenario())
    assert len(fixture.onebot.send_calls) == 1, (
        "只有第一个投递应真正发送：第二个调用因客户端身份不符被服务端拒绝",
        fixture.onebot.send_calls,
    )
    fixture.close()


def test_卸载撤销多个尝试有总时长上限且如实报告未确认(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6.8 复核修复：整段远端撤销共享总 deadline，不随尝试数线性放大等待。

    两个待撤销尝试的撤销请求都悬挂：修复前会按每个 `cancel` RPC 的 10 秒上界串行
    等待（最坏 N × 10 秒）；修复后整段共享总上限（本用例缩短为 0.05 秒），超出的
    尝试不再请求，并按「撤销未确认、不宣称远端已停止」如实告警。
    """

    monkeypatch.setattr(plugin_module, "REVOKE_TOTAL_WAIT_SECONDS", 0.05)
    fixture = make_client_service(tmp_path, request_timeout_seconds=60.0)
    store_source_message(
        fixture.onebot,
        message_id="m-source-2",
        group_id=SOURCE_GROUP_ID,
        message=onebot_segments([{"type": "text", "data": "另一条源消息"}]),
    )
    cancel_paths: list[str] = []
    source_paths: list[str] = []
    original_request = fixture.transport.request

    async def hanging_request(method: str, url: str, **kwargs: Any) -> Any:
        if url.endswith("/cancel"):
            cancel_paths.append(url)
            await asyncio.Event().wait()
        if url.endswith("/source"):
            source_paths.append(url)
            await asyncio.Event().wait()
        return await original_request(method, url, **kwargs)

    fixture.transport.request = hanging_request  # type: ignore[method-assign]
    first = issue_source_call(fixture.plugin, message_id="m-source-1")
    second = issue_source_call(fixture.plugin, message_id="m-source-2")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        # 先让第一个投递进入源阶段（尝试已登记），再装配第二条宿主源消息并发第二个：
        # 避免并发建立会话互相替换，两个尝试稳定共存。
        first_task = asyncio.create_task(fixture.plugin.forward_message(**first))
        await wait_until(lambda: len(source_paths) == 1)
        prepare_source(fixture.ctx, message_id="m-source-2")
        second_task = asyncio.create_task(fixture.plugin.forward_message(**second))
        await wait_until(lambda: len(source_paths) == 2)
        tasks = [first_task, second_task]
        await asyncio.wait_for(fixture.plugin.on_unload(), timeout=5.0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())

    warnings = "\n".join(fixture.ctx.logger.texts("warning"))
    assert cancel_paths, "撤销必须被尝试过（至少一个尝试发起撤销请求）"
    assert len(cancel_paths) <= 1, (
        "整段撤销必须共享总 deadline：总上限用尽后不得再逐条请求",
        cancel_paths,
    )
    assert warnings.count("不宣称远端已停止") >= 2, (
        "两个未确认的撤销都必须如实告警，不得静默",
        fixture.ctx.logger.records,
    )
    assert fixture.onebot.send_count == 0, "撤销阶段不得产生任何平台发送"
    fixture.close()
