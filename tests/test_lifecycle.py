"""公开生命周期：卸载只结束本插件自己的工作，任务表定位告警与五秒共享预算。

断言面只经公共入口：`create_plugin` 装配的插件实例、注册 Hook 与
`forward_message` 工具；账本用真实 SQLite 账本，平台动作走内存传输替身
（`tests/snowluma_client_support`）。覆盖 5.1 卸载路径：取消并有界等待本插件
自己的任务（自动等待与在途投递）、不调用任何连接关闭、任务表告警带源消息／
目标群／阶段、账本关闭与其它清理共享同一五秒预算且只执行一次；预检期间与
卸载后到达的调用都不得再发送。另覆盖组合根的连接签名（SNOWLUMA 地址）替换：
端点变化后旧客户端／旧 aiohttp 会话被关闭且新客户端独立投递；旧客户端关闭
等待期间卸载不新建投递也不重复关闭，随后的投递改走新会话、不被旧会话续发。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from maimai_courier_plugin.shared.delivery import STATUS_SENT
from maimai_courier_plugin.shared.result import COMMENT_DELAY_SECONDS
from tests.direct_support import DirectFixture, merged_forward_message
from tests.fake_maibot_sdk import FakePluginContext
from tests.snowluma_client_support import TARGET_MESSAGE_ID
from tests.support import (
    HTTP_URL,
    MESSAGE_ID,
    SOURCE_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    FakeClock,
    enabled_config,
    issue_source_call,
    make_plugin,
    open_test_ledger,
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
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
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
    plugin, ctx = make_plugin(tmp_path, clock=clock, config=enabled_config())
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
