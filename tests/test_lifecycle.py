"""公开生命周期：卸载只结束本插件自己的工作，任务表定位告警与五秒共享预算。

断言面只经公共入口：`create_plugin` 装配的插件实例、注册 Hook 与
`forward_message` 工具；账本用真实 SQLite 账本，平台动作走内存传输替身
（`tests/snowluma_client_support`）。覆盖 5.1 卸载路径：取消并有界等待本插件
自己的任务（自动等待与在途投递）、不调用任何连接关闭、任务表告警带源消息／
目标群／阶段、账本关闭与其它清理共享同一五秒预算且只执行一次；预检期间与
卸载后到达的调用都不得再发送。另覆盖组合根的连接签名（SNOWLUMA 地址）替换：
端点变化后旧客户端／旧 aiohttp 会话被关闭且新客户端独立投递；旧客户端关闭
等待期间卸载不新建投递也不重复关闭，随后的投递改走新会话、不被旧会话续发。
还覆盖预搜索的生命周期与预算：候选就绪后新开的只在读查询窗口内，在途只读查询
可被卸载取消且任务有界退出，候选失效按既有口径取消，仅改标准不取消仍合法候选
并按最新标准唤起。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from maimai_courier_plugin.shared.delivery import STATUS_SENT
from maimai_courier_plugin.shared.result import COMMENT_DELAY_SECONDS
from tests.direct_support import (
    DirectFixture,
    apply_config_update,
    drive_observe_hook,
    intent_criteria_region,
    make_auto_plugin,
    make_direct_plugin,
    merged_forward_message,
)
from tests.fake_maibot_sdk import FakePluginContext
from tests.snowluma_client_support import TARGET_MESSAGE_ID
from tests.support import (
    CONTROL_CHARACTER_GROUP_NAME,
    HTTP_URL,
    MESSAGE_ID,
    SOURCE_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    FakeClock,
    enabled_config,
    forward_node,
    forward_segment,
    issue_source_call,
    open_test_ledger,
    source_message,
    source_stream,
    spin_until,
)

NEW_HTTP_URL = "http://127.0.0.1:3002"
"""端点替换用的新回环 HTTP 地址：仍合法，但连接签名不同于默认地址（触发客户端替换）。"""


def config_with_endpoint(endpoint: str) -> dict[str, Any]:

    config = enabled_config()
    config["snowluma"] = {"http_url": endpoint, "token": "合成令牌"}
    return config


async def apply_endpoint_update(plugin: Any, endpoint: str) -> None:

    config = config_with_endpoint(endpoint)
    plugin.set_plugin_config(config)
    await plugin.on_config_update("self", config, "")


def _uncooperative(event: asyncio.Event, release: asyncio.Event) -> Any:

    async def hold(*args: Any, **kwargs: Any) -> None:
        event.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return None
    return hold


class SwallowingClock(FakeClock):
    """评论延迟等待吞掉取消并停在专用事件上：模拟不合作的在途评论等待点。

    只对 `COMMENT_DELAY_SECONDS` 的等待生效（动作看门狗、卸载预算与回执轮询的
    等待行为不变），`unstuck()` 后恢复；用于验证卸载五秒预算耗尽时的定位告警。
    """

    def __init__(self) -> None:
        super().__init__()
        self.stuck: asyncio.Future[None] | None = None
        self.release = False

    async def sleep(self, seconds: float) -> None:
        if seconds != COMMENT_DELAY_SECONDS or self.release:
            await super().sleep(seconds)
            return
        self.sleeps.append(float(seconds))
        while not self.release:
            try:
                await self._stuck_wait()
                return
            except asyncio.CancelledError:
                continue

    async def _stuck_wait(self) -> None:
        self.stuck = asyncio.get_running_loop().create_future()
        await self.stuck

    def unstuck(self) -> None:

        self.release = True
        if self.stuck is not None and not self.stuck.done():
            self.stuck.set_result(None)


# --------------------------------------------------------------------------- 5.1 卸载路径


def test_卸载取消尚未发送的自动等待并等待任务退出(tmp_path: Path) -> None:

    clock = FakeClock()
    plugin, ctx = make_auto_plugin(tmp_path, clock=clock, config=enabled_config())
    prepare_candidate_source(ctx, message_id="m-broadcast-1")
    payload = merged_forward_message(message_id="m-broadcast-1")
    foreign_release = asyncio.Event()
    foreign_finished: list[str] = []

    async def foreign_work() -> None:

        await foreign_release.wait()
        foreign_finished.append("done")

    async def scenario() -> None:
        await plugin.on_load()
        await plugin.observe_received_message(**payload)
        await spin_until(lambda: bool(clock.sleeps))
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
        assert "已卸载" in "\n".join(ctx.logger.texts("info")), ("卸载必须等待被取消的任务退出并留下完成记录", ctx.logger.records)

        foreign_release.set()
        await foreign
        assert foreign_finished == ["done"], "其他插件的任务在卸载后应能正常完成"

    asyncio.run(scenario())


def prepare_candidate_source(ctx: FakePluginContext, *, message_id: str) -> None:

    ctx.chat.streams = [source_stream()]
    ctx.message.store = {}


def test_卸载取消在途投递并等待任务退出(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin, comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await spin_until(lambda: COMMENT_DELAY_SECONDS in fixture.clock.sleeps)

        await fixture.plugin.on_unload()

        for _ in range(50):
            await asyncio.sleep(0)
        assert task.done(), "卸载必须取消并等待在途投递任务退出（不得让它在后台继续）"
        result = await task
        assert result["status"] == "partial", result
        assert not any("未退出" in text for text in fixture.ctx.logger.texts("warning")), (
            "被取消的投递任务在卸载等待上限内退出，不得留下「未退出」告警",
            fixture.ctx.logger.records,
        )
        assert "已卸载" in "\n".join(fixture.ctx.logger.texts("info")), (
            "卸载必须等待被取消的投递任务退出并留下完成记录",
            fixture.ctx.logger.records,
        )
        assert [call["action"] for call in fixture.transport.sent] == ["send_group_forward_msg"], (
            "卸载后不得提交评论发送",
        )

    asyncio.run(scenario())

    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT, "已成功发送的源消息事实不得因卸载回滚"
    assert record["target_message_id"] == TARGET_MESSAGE_ID
    fixture.ledger.close()


def test_卸载不调用任何连接关闭只等待自己的任务(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)
    closed: list[str] = []
    original_close = fixture.transport.close

    async def recording_close() -> None:
        closed.append("close")
        await original_close()

    async def scenario() -> None:
        await fixture.plugin.on_load()
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"
        record = fixture.record()
        assert record is not None and record["status"] == STATUS_SENT
        fixture.transport.close = recording_close

        await fixture.plugin.on_unload()

        assert closed == [], "卸载路径不得调用任何连接关闭（ws 关闭已随通道替换移除）"
        assert fixture.transport.closed is False, "卸载不得关闭传输会话"
        assert fixture.close_calls == ["close"], "卸载仍须在共享预算内关闭本插件账本"
        info = "\n".join(fixture.ctx.logger.texts("info"))
        assert "已卸载" in info

    asyncio.run(scenario())
    fixture.ledger.close()


def test_卸载等待超时告警定位投递任务且不再发起新的发送(tmp_path: Path) -> None:

    clock = SwallowingClock()
    fixture = DirectFixture(tmp_path, clock=clock)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin, comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await spin_until(lambda: COMMENT_DELAY_SECONDS in fixture.clock.sleeps)
        unloading = asyncio.create_task(fixture.plugin.on_unload())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            fixture.clock.advance(5)
            await unloading
            warnings = "\n".join(fixture.ctx.logger.texts("warning"))
            assert "未退出" in warnings, warnings
            assert "m-source-1" in warnings, warnings
            assert TARGET_GROUP_ID in warnings, warnings
            assert "投递" in warnings and "阶段=评论等待" in warnings, warnings
            assert [call["action"] for call in fixture.transport.sent] == ["send_group_forward_msg"], (
                "卸载后不得提交评论发送",
            )
            assert fixture.ctx.send.calls == []
            assert fixture.ctx.maisaka.proactive.calls == []
        finally:
            clock.unstuck()
            for _ in range(50):
                await asyncio.sleep(0)
        assert task.done() and (await task)["status"] == "partial", "放行后按已知源事实报告部分成功"

    asyncio.run(scenario())
    fixture.ledger.close()


def test_卸载等待超时告警定位自动判断任务(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_auto_plugin(tmp_path, clock=clock, config=enabled_config())
    prepare_candidate_source(ctx, message_id="m-stuck")
    entered, release = asyncio.Event(), asyncio.Event()
    ctx.message.get_by_id = _uncooperative(entered, release)

    async def scenario() -> None:
        await plugin.observe_received_message(**merged_forward_message(message_id="m-stuck"))
        await spin_until(entered.is_set)
        unloading = asyncio.create_task(plugin.on_unload())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            clock.advance(5)
            await unloading
            warnings = "\n".join(ctx.logger.texts("warning"))
            assert "未退出" in warnings and "m-stuck" in warnings and "自动判断" in warnings, warnings
            assert ctx.send.calls == [] and ctx.maisaka.proactive.calls == []
        finally:
            release.set()
            for _ in range(50):
                await asyncio.sleep(0)

    asyncio.run(scenario())


def test_卸载后到达的工具调用不再发送(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)

    async def scenario() -> dict[str, Any]:
        await fixture.plugin.on_load()
        await fixture.plugin.on_unload()
        return await fixture.plugin.forward_message(**arguments)

    result = asyncio.run(scenario())

    assert result["status"] == "rejected", result
    assert "已卸载" in str(result["reason"]), result
    assert "本次未发送任何消息" in str(result["reason"]), result
    assert fixture.constructed == [], "卸载后不得再构造传输"
    assert fixture.ctx.send.calls == []
    assert fixture.ctx.chat.calls == [], "卸载后不得再查询宿主会话"
    fixture.ledger.close()


def test_预检期间卸载时不再创建发送任务(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)
    original_get_by_id = fixture.ctx.message.get_by_id

    async def unloading_get_by_id(*args: Any, **kwargs: Any) -> Any:

        await fixture.plugin.on_unload()
        return await original_get_by_id(*args, **kwargs)

    fixture.ctx.message.get_by_id = unloading_get_by_id

    async def scenario() -> None:
        await fixture.plugin.on_load()
        result = await fixture.plugin.forward_message(**arguments)
        assert result["status"] == "rejected", result
        assert "本次未发送任何消息" in str(result["reason"]), result
        assert fixture.transport.sent == [], "预检期间卸载不得创建发送任务"
        assert fixture.constructed == []

    asyncio.run(scenario())
    fixture.ledger.close()


# --------------------------------------------------------------------------- 5.1 五秒共享预算


def test_卸载在五秒预算内关闭账本且只执行一次(tmp_path: Path) -> None:
    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)

    async def scenario() -> None:
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"
        await fixture.plugin.on_unload()
        await fixture.plugin.on_unload()
        assert fixture.close_calls == ["close"], "重复卸载不得重复关闭账本"
        info = "\n".join(fixture.ctx.logger.texts("info"))
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        assert "已卸载" in info and warnings == "", (info, warnings)
        # 真实关闭完成后才允许新实例取得使用权：open_test_ledger 内部即断言可打开。
        reopened = open_test_ledger(fixture.data_dir, clock=fixture.clock)
        reopened.close()

    asyncio.run(scenario())
    fixture.ledger.close()


def test_账本关闭失败只告警不宣称退出(tmp_path: Path) -> None:
    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)
    sensitive = "synthetic-token-and-message-body"

    async def scenario() -> None:
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"

        def failing_close() -> None:
            fixture.close_calls.append("close")
            raise RuntimeError(sensitive)

        fixture.storage.close = failing_close
        await fixture.plugin.on_unload()
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        info = "\n".join(fixture.ctx.logger.texts("info"))
        assert "账本关闭" in warnings and "未确认" in warnings and "不宣称全部退出" in warnings, warnings
        assert sensitive not in warnings + info, "日志不得回显存储异常载荷"
        assert "已卸载" not in info

    asyncio.run(scenario())
    fixture.ledger.close()


def test_同步账本关闭跨过五秒预算仍报告超时(tmp_path: Path) -> None:
    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)
    release = threading.Event()

    async def scenario() -> None:
        assert (await fixture.plugin.forward_message(**arguments))["status"] == "sent"

        def blocking_close() -> None:
            fixture.close_calls.append("close")
            release.wait()
            fixture.ledger.close()

        fixture.storage.close = blocking_close
        unload = asyncio.create_task(fixture.plugin.on_unload())
        await spin_until(lambda: fixture.close_calls == ["close"])
        fixture.clock.advance(6.0)
        release.set()
        await unload
        warnings = "\n".join(fixture.ctx.logger.texts("warning"))
        info = "\n".join(fixture.ctx.logger.texts("info"))
        assert "超出五秒总预算" in warnings and "不宣称全部退出" in warnings, warnings
        assert "已卸载" not in info

    asyncio.run(scenario())
    fixture.ledger.close()


# --------------------------------------------------------------------------- 组合根：连接签名替换


def test_配置签名变化时旧客户端关闭且新客户端独立(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    first = issue_source_call(fixture.plugin)
    second = issue_source_call(fixture.plugin, message_id="m-new")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        assert (await fixture.plugin.forward_message(**first))["status"] == "sent"
        old_transport = fixture.transports[0]
        close_calls: list[str] = []
        original_close = old_transport.close

        async def counting_close() -> None:
            close_calls.append("close")
            await original_close()

        old_transport.close = counting_close

        await apply_endpoint_update(fixture.plugin, NEW_HTTP_URL)

        # forward 1 已结束：切到 m-new 的宿主源查询，并预置新端点传输要返回的源记录。
        fixture.prepare_source(message_id="m-new")
        fixture.stage_next_source(message_id="m-new")
        result = await fixture.plugin.forward_message(**second)

        assert result["status"] == "sent", result
        assert close_calls == ["close"], "端点替换必须关闭旧客户端，且旧传输 close 只调一次"
        assert old_transport.closed is True, "旧客户端关闭后其传输会话应已关闭"
        new_transport = fixture.transports[-1]
        assert new_transport is not old_transport, "新端点必须为新投递构造独立传输会话"
        assert new_transport.closed is False, "正在服务新投递的会话不得被关闭"
        assert [item["action"] for item in new_transport.sent] == ["send_group_forward_msg"], (
            "端点替换后的新投递必须经由新会话发送一次",
        )

    asyncio.run(scenario())
    assert len(fixture.transports) == 2, "端点替换只应新建一条传输会话，不得反复构造"
    fixture.ledger.close()


def test_旧客户端关闭等待期间卸载不新建投递也不重复关闭(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    first = issue_source_call(fixture.plugin)
    second = issue_source_call(fixture.plugin, message_id="m-new")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        assert (await fixture.plugin.forward_message(**first))["status"] == "sent"
        old_transport = fixture.transports[0]
        close_calls: list[str] = []
        entered = asyncio.Event()
        release = asyncio.Event()
        original_close = old_transport.close

        async def held_close() -> None:
            close_calls.append("close")
            entered.set()
            await release.wait()
            await original_close()

        old_transport.close = held_close

        await apply_endpoint_update(fixture.plugin, NEW_HTTP_URL)

        fixture.prepare_source(message_id="m-new")
        task = asyncio.create_task(fixture.plugin.forward_message(**second))
        await spin_until(entered.is_set)  # 端点替换触发旧客户端关闭并停在等待点

        unloading = asyncio.create_task(fixture.plugin.on_unload())
        await spin_until(task.done)  # 卸载取消在途投递：不等旧关闭完成即拒绝
        release.set()
        await unloading
        result = await task
        assert result["status"] == "rejected", result
        assert "旧连接关闭未确认" in str(result["reason"]), result
        assert close_calls == ["close"], "旧客户端 close 只调一次，卸载竞态不得重复关闭"
        assert len(fixture.transports) == 1, "关闭等待期间卸载不得为新端点构造传输（不新建投递）"
        assert [item["action"] for item in old_transport.sent] == ["send_group_forward_msg"], (
            "被拒绝的新投递不得借旧会话发送",
        )

    asyncio.run(scenario())
    fixture.ledger.close()


def test_端点替换后新投递改走新会话且旧会话不再续发(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    first = issue_source_call(fixture.plugin)
    second = issue_source_call(fixture.plugin, message_id="m-new")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        assert (await fixture.plugin.forward_message(**first))["status"] == "sent"
        old_transport = fixture.transports[0]
        close_calls: list[str] = []
        original_close = old_transport.close

        async def counting_close() -> None:
            close_calls.append("close")
            await original_close()

        old_transport.close = counting_close
        old_sent = [dict(item) for item in old_transport.sent]
        old_queries = list(old_transport.queries)

        await apply_endpoint_update(fixture.plugin, NEW_HTTP_URL)

        fixture.prepare_source(message_id="m-new")
        fixture.stage_next_source(message_id="m-new")
        result = await fixture.plugin.forward_message(**second)

        assert result["status"] == "sent", result
        assert close_calls == ["close"], "旧客户端 close 只调一次（不重复、不漏关）"
        assert old_transport.queries == old_queries, "旧会话被关闭后不得再收到新查询"
        assert old_transport.sent == old_sent, "旧会话被关闭后不得再代发新投递"
        new_transport = fixture.transports[-1]
        assert new_transport is not old_transport and new_transport.closed is False
        assert ("get_msg", {"message_id": "m-new"}) in new_transport.queries, ("新投递的源查询应落在新会话")
        assert [item["action"] for item in new_transport.sent] == ["send_group_forward_msg"], (
            "端点替换后的新投递必须由新会话发送",
        )

    asyncio.run(scenario())
    fixture.ledger.close()


def test_分组收紧后评论提交前复验失败且不回滚已投递源(tmp_path: Path) -> None:

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin, comment="收紧后不应发送的评论")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await spin_until(lambda: COMMENT_DELAY_SECONDS in fixture.clock.sleeps)
        tightened = enabled_config(
            groups=[{"name": "研发", "source_groups": [SOURCE_GROUP_ID], "target_groups": []}],
        )
        await apply_config_update(fixture.plugin, tightened)
        fixture.clock.advance(COMMENT_DELAY_SECONDS)
        result = await asyncio.wait_for(task, 5)

        assert result["status"] == "partial", result
        assert result["comment_status"] == "failed", result
        assert [item["action"] for item in fixture.transport.sent] == ["send_group_forward_msg"], (
            "目标被移出源组后不得提交评论发送",
        )
        record = fixture.record()
        assert record is not None and record["status"] == STATUS_SENT, record
        assert record["comment_status"] == "", record

    asyncio.run(scenario())
    fixture.ledger.close()


def test_卸载告警的任务描述带压平分组前缀(tmp_path: Path) -> None:

    clock = SwallowingClock()
    fixture = DirectFixture(tmp_path, clock=clock, groups=[
        {"name": CONTROL_CHARACTER_GROUP_NAME, "source_groups": [SOURCE_GROUP_ID],
         "target_groups": [TARGET_GROUP_ID]},
    ])
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin, comment="等待期间卸载")

    async def scenario() -> None:
        await fixture.plugin.on_load()
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await spin_until(lambda: COMMENT_DELAY_SECONDS in fixture.clock.sleeps)
        unloading = asyncio.create_task(fixture.plugin.on_unload())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            fixture.clock.advance(5)
            await unloading
            warnings = "\n".join(fixture.ctx.logger.texts("warning"))
            assert "分组=研发 甲" in warnings, warnings
            for text in fixture.ctx.logger.texts("warning"):
                assert "\n" not in text and "\x00" not in text, text
        finally:
            clock.unstuck()
            for _ in range(50):
                await asyncio.sleep(0)
        assert task.done() and (await task)["status"] == "partial", "放行后按已知源事实报告部分成功"

    asyncio.run(scenario())
    fixture.ledger.close()


def test_源提交前分组收紧按拒绝处理且不发送不落账(tmp_path: Path) -> None:
    """目标被移出来源群分组后，源提交前的许可复验（`_send_allowed`）整条拒绝。"""

    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin, comment="不应发送的评论")
    tightened = enabled_config(
        groups=[{"name": "研发", "source_groups": [SOURCE_GROUP_ID], "target_groups": []}],
    )
    applied = False

    async def tighten_on_prepare(action: str, params: Any) -> Any:
        # 预检通过后、源查询（同账号名册与源消息）期间收紧分组：源提交前复验必须拦截。
        nonlocal applied
        if action == "get_login_info" and not applied:
            applied = True
            await apply_config_update(fixture.plugin, tightened)
        return None

    fixture.transport.on_action = tighten_on_prepare

    async def scenario() -> None:
        await fixture.plugin.on_load()
        result = await fixture.plugin.forward_message(**arguments)
        assert result["status"] == "rejected", result
        assert "本次未发送任何消息" in str(result["reason"]), result
        assert fixture.transport.sent == [], "分组收紧后不得提交源发送"
        assert fixture.record() is None, "未发送不得落账"

    asyncio.run(scenario())
    fixture.ledger.close()


def test_on_config_update只取消失效自动判断且不打断合法候选(tmp_path: Path) -> None:
    clock = FakeClock()
    groups = [
        {"name": "研发", "source_groups": [SOURCE_GROUP_ID], "target_groups": [TARGET_GROUP_ID]},
        {"name": "运营", "source_groups": ["555"], "target_groups": ["666"]},
    ]
    plugin, ctx = make_auto_plugin(tmp_path, clock=clock, config=enabled_config(groups=groups))
    candidate_a = merged_forward_message(message_id="m-auto-a", group_id=SOURCE_GROUP_ID,
                                         session_id="s-a")
    candidate_b = merged_forward_message(message_id="m-auto-b", group_id="555", session_id="s-b")
    # 收紧研发分组（源 111 的目标被移出），运营分组保持不变：A 失效、B 仍合法。
    tightened = enabled_config(groups=[
        {"name": "研发", "source_groups": [SOURCE_GROUP_ID], "target_groups": []},
        {"name": "运营", "source_groups": ["555"], "target_groups": ["666"]},
    ])

    def queries(message_id: str) -> int:
        return sum(1 for call in ctx.message.calls if call["message_id"] == message_id)

    async def scenario() -> None:
        await plugin.observe_received_message(**candidate_a)
        await plugin.observe_received_message(**candidate_b)
        await spin_until(lambda: len(clock.sleeps) >= 1 and len(ctx.message.calls) >= 2)
        await apply_config_update(plugin, tightened)
        for _ in range(50):
            await asyncio.sleep(0)
        a_before, b_before = queries("m-auto-a"), queries("m-auto-b")
        # 收紧后消息仍未就绪：被取消的 A 停止轮询，仍合法的 B 继续有限的就绪轮询。
        clock.advance(2.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert queries("m-auto-a") == a_before, "失效候选被热更新取消，停止就绪轮询"
        assert queries("m-auto-b") > b_before, "仍合法的候选不被打断，继续就绪轮询"
        # 两条候选此刻都可读取：只有仍合法的 B 被唤起。
        ctx.message.default_result = source_message(message_id="m-auto-any")
        clock.advance(20.0)
        for _ in range(100):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    triggered = [call["metadata"]["source_message_id"] for call in ctx.maisaka.proactive.calls]
    assert triggered == ["m-auto-b"], "热更新只取消失效的自动判断，仍合法的候选照常唤起"


# --------------------------------------------------------------------------- 组合根：预搜索的生命周期与预算
# 预搜索是合并转发候选「就绪 → 目标名称查询」之间新增的只在读查询窗口（design D5）：
# 卸载与热更新都必须在这个新窗口内保持既有口径——在途只读查询可被取消、任务有界
# 退出；预搜索期间候选失效按既有口径取消、仅改标准不取消仍合法候选。断言只经公开
# 入口（观察 Hook / 工厂装配）与宿主能力调用记录，平台动作走 `DirectFixture` 内存
# 传输替身，单测零真实联网；其中私有任务表（`_tasks` / `_auto_tasks`）仅作辅助佐证，
# 行为证据以公开入口断言为准。


def inflight_expand_fixture(
    tmp_path: Path,
    *,
    cancelled: list[bool] | None = None,
) -> tuple[Any, dict[str, Any], asyncio.Event, asyncio.Event]:
    """装配「预搜索在途」现场：候选就绪后的只在读展开查询挂在可取消等待上。

    返回（插件夹具, 合并转发挥载, 已进入展开查询的事件, 放行展开查询的事件）。
    展开查询等在一个可取消的真实异步等待上，取消须如实到达该查询（记录后再抛出），
    再由场景放行；需要断言「查询被取消」的用例传入 `cancelled` 列表。
    """

    fixture = make_direct_plugin(tmp_path, raw_message=[forward_segment(forward_node())])
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_expand(action: str, params: Any) -> Any:
        # 只在读展开查询处挂起（预搜索在途）：等在一个可取消的真实异步等待上，
        # 取消须如实到达该查询（记录后再抛出），测试收尾再放行。
        if action == "get_forward_msg":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                if cancelled is not None:
                    cancelled.append(True)
                raise
        return None

    fixture.onebot.on_action = hold_expand
    return fixture, merged_forward_message(message_id=MESSAGE_ID), entered, release


def test_卸载取消在途预搜索只读查询并有界退出(tmp_path: Path) -> None:
    """预搜索只在读展开查询在途时卸载：查询被取消、任务有界退出、不留悬挂会话。"""

    query_cancelled: list[bool] = []
    fixture, payload, entered, release = inflight_expand_fixture(tmp_path, cancelled=query_cancelled)

    async def scenario() -> None:
        await drive_observe_hook(fixture.plugin, fixture.clock, payload, until=entered.is_set)
        assert entered.is_set(), "候选就绪后应进入预搜索的只在读展开查询"

        # 不推进注入时钟：任务必须靠取消自行退出，卸载才能在五秒预算内收尾，
        # 而不是等注入时钟的定时器到点。
        await asyncio.wait_for(fixture.plugin.on_unload(), timeout=2.0)
        release.set()
        for _ in range(50):
            await asyncio.sleep(0)

        assert query_cancelled == [True], "在途预搜索只读查询须被卸载取消（取消如实到达展开查询）"
        warnings = fixture.ctx.logger.texts("warning")
        info = "\n".join(fixture.ctx.logger.texts("info"))
        assert "已卸载" in info, (info, fixture.ctx.logger.records)
        assert not any("未退出" in text for text in warnings), (
            "预搜索只读查询可被取消，自动判断任务应在五秒预算内退出", warnings,
        )
        assert not any("超出五秒总预算" in text for text in warnings), warnings
        assert not any("保持账本独占" in text for text in warnings), warnings
        assert not any("不宣称全部退出" in text for text in warnings), warnings
        assert fixture.plugin._tasks == {}, "卸载后本插件任务表无残留（预搜索任务有界退出）"
        assert fixture.plugin._auto_tasks == {}, "卸载后无残留的自动判断任务"
        assert fixture.ctx.maisaka.proactive.calls == [], "在途预搜索被取消，未提供判断机会"
        assert fixture.onebot.sent == [], "预搜索只读，卸载不提交任何发送"
        assert fixture.transport.closed is False, "卸载不关闭连接会话，也不留悬挂会话"

        # 卸载后无新增常驻任务：后续候选被卸载标记拦在入口，不登记后台任务、不唤起。
        await fixture.plugin.observe_received_message(**merged_forward_message(message_id="m-after-unload"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert fixture.plugin._tasks == {}, "卸载后不得新增常驻任务"
        assert fixture.ctx.maisaka.proactive.calls == [], "卸载后不提供新的判断机会"

    asyncio.run(scenario())
    fixture.ledger.close()


@pytest.mark.parametrize(
    "label, invalidated",
    [
        ("候选类型被移出", enabled_config(message_types=("text",))),
        ("总开关关闭", {**enabled_config(), "plugin": {"enabled": False}}),
        (
            "分组收紧",
            enabled_config(groups=[{"name": "研发", "source_groups": [SOURCE_GROUP_ID],
                                    "target_groups": []}]),
        ),
    ],
)
def test_预搜索在途时热更新使候选失效则不唤起(
    tmp_path: Path, label: str, invalidated: dict[str, Any],
) -> None:
    """预搜索只读查询在途期间候选失效（类型移出／开关关闭／分组收紧）：按既有口径取消，零唤起。"""

    fixture, payload, entered, release = inflight_expand_fixture(tmp_path)

    async def scenario() -> None:
        await drive_observe_hook(fixture.plugin, fixture.clock, payload, until=entered.is_set)
        assert entered.is_set()
        await apply_config_update(fixture.plugin, invalidated)  # 预搜索在途：候选此刻失效
        for _ in range(50):
            await asyncio.sleep(0)
        release.set()
        for _ in range(50):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert fixture.ctx.maisaka.proactive.calls == [], (label, "失效候选零唤起")
    assert fixture.plugin._auto_tasks == {}, (label, "失效的自动判断任务已被取消，无残留")
    assert fixture.onebot.sent == [], (label, "取消预搜索不得提交任何发送")
    fixture.close()


def test_预搜索在途时仅改标准不取消候选且按最新标准唤起(tmp_path: Path) -> None:
    """预搜索只读查询在途期间只改自动分享标准：候选仍合法、不被取消，照常按最新标准唤起。"""

    fixture, payload, entered, release = inflight_expand_fixture(tmp_path)
    latest = "只改标准：偏好工具与话题，保留换行"

    async def scenario() -> None:
        await drive_observe_hook(fixture.plugin, fixture.clock, payload, until=entered.is_set)
        assert entered.is_set()
        # 只改标准（不改总开关／候选类型／转发分组）：候选仍合法，热更新不得取消它。
        await apply_config_update(fixture.plugin, enabled_config(share_criteria=latest))
        for _ in range(50):
            await asyncio.sleep(0)
        assert fixture.ctx.maisaka.proactive.calls == [], "预搜索在途期间尚未唤起"
        release.set()  # 放行预搜索：展开无嵌套 → 进入既有唤起流程
        for _ in range(100):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(fixture.ctx.maisaka.proactive.calls) == 1, "仅改标准仍按最新配置唤起一次"
    intent = fixture.ctx.maisaka.proactive.calls[0]["intent"]
    assert intent_criteria_region(intent) == latest, "唤起使用最新编辑后的标准原文"
    assert fixture.onebot.sent == [], "自动判断只唤起，不提交任何发送"
    fixture.close()
