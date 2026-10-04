"""2.6 可控 HTTP、OneBot 与时钟测试设施：分阶段暂停、故障注入与时钟推进。

验收判据（本组原文）：

- 能分别暂停准备、平台提交、回执、落账与评论阶段（暂停期间任务确实停在阶段入口，
  放行后继续完成）；
- 能注入断线（后续调用明确不可用）与错误响应（原样返回、不自动重试）；
- 记录查询/发送调用次数与目标；
- 超时通过注入时钟推进，不真实等待；
- 默认只在本进程内存里驱动（`memory://onebot`），绝不连接真实群。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.service_support import (
    IN_MEMORY_ENDPOINT,
    STAGE_COMMENT,
    STAGE_LEDGER_WRITE,
    STAGE_NAMES,
    STAGE_PLATFORM_COMMIT,
    STAGE_PREPARE,
    STAGE_RECEIPT,
    FakeClock,
    OneBotUnavailable,
    ServiceHarness,
    StageGates,
    launch_service,
    pump,
    walk_all_stages,
)


def test_设施默认只在内存里驱动不连接真实群() -> None:
    harness = ServiceHarness()

    assert harness.onebot.endpoint == IN_MEMORY_ENDPOINT
    response = harness.client().call("GET", "/v1/forwards/d-1")
    assert response.status == 200, response
    assert harness.onebot.send_count == 0, "没有发送调用就不得出现发送计数"

    assert set(STAGE_NAMES) == {
        STAGE_PREPARE,
        STAGE_PLATFORM_COMMIT,
        STAGE_RECEIPT,
        STAGE_LEDGER_WRITE,
        STAGE_COMMENT,
    }, "五个可暂停阶段就是本组验收列出的阶段"


def test_五个阶段可分别暂停与放行() -> None:
    async def scenario(stage: str) -> dict[str, Any]:
        harness = ServiceHarness()
        harness.gates.pause(stage)
        task = asyncio.ensure_future(walk_all_stages(harness))
        await pump(lambda: harness.gates.arrivals.get(stage, 0) >= 1)
        paused = not task.done()
        assert task.done() is False, f"{stage} 阶段必须真的停在入口"
        harness.gates.release(stage)
        await pump(lambda: task.done())
        error = None if task.cancelled() else task.exception()
        if error is not None:
            raise error
        return {"paused": paused, "completed": task.done()}

    for stage in STAGE_NAMES:
        outcome = asyncio.run(scenario(stage))
        assert outcome == {"paused": True, "completed": True}, (stage, outcome)


def test_记录查询与发送调用次数与目标() -> None:
    async def scenario() -> ServiceHarness:
        harness = ServiceHarness()
        await harness.onebot.send_msg(
            target_group_id="20002",
            message=[{"type": "forward", "data": {"id": "res-1"}}],
        )
        await harness.onebot.send_msg(
            target_group_id="20003",
            message=[{"type": "text", "data": {"text": "评论"}}],
            stage=STAGE_COMMENT,
        )
        await harness.onebot.get_msg("m-source-1")
        await harness.onebot.get_login_info()
        return harness

    harness = asyncio.run(scenario())

    assert harness.onebot.send_count == 2
    assert [call.target for call in harness.onebot.send_calls] == ["20002", "20003"]
    assert harness.onebot.query_counts == {"get_msg": 1, "get_login_info": 1}
    assert harness.onebot.calls[0].operation == "send_msg"


def test_注入断线与错误响应() -> None:
    async def scenario() -> None:
        harness = ServiceHarness()

        harness.onebot.fail_next(
            "send_msg",
            {"status": "failed", "retcode": 100, "message": "平台拒绝发送"},
        )
        rejected = await harness.onebot.send_msg(
            target_group_id="20002",
            message=[{"type": "text", "data": {"text": "x"}}],
        )
        assert rejected["status"] == "failed", rejected
        assert harness.onebot.send_count == 1, "注入失败也是一次真实到达平台的调用"

        harness.onebot.disconnect()
        for operation in (
            harness.onebot.send_msg(
                target_group_id="20002",
                message=[{"type": "text", "data": {"text": "x"}}],
            ),
            harness.onebot.get_msg("m-source-1"),
            harness.onebot.get_login_info(),
        ):
            with pytest.raises(OneBotUnavailable):
                await operation

        harness.onebot.reconnect()
        missing = await harness.onebot.get_msg("m-source-1")
        assert missing["status"] == "failed", "缓存缺失必须明确失败，不能伪造内容"

    asyncio.run(scenario())


def test_超时用注入时钟推进而不真实等待() -> None:
    async def scenario() -> None:
        harness = ServiceHarness()
        harness.gates.pause(STAGE_RECEIPT)

        waiter = asyncio.ensure_future(
            harness.gates.wait_for(STAGE_RECEIPT, clock=harness.clock, timeout_seconds=30.0)
        )
        await pump(lambda: harness.clock.sleeps == [30.0])
        assert not waiter.done(), "未推进时钟前不得超时"
        assert waiter.done() is False

        harness.clock.advance(30.0)
        await pump(lambda: waiter.done())
        assert waiter.result() is False, "阶段始终没被走到，必须按超时返回"

    asyncio.run(scenario())


def test_时钟替身复用现有实现() -> None:
    from tests.support import FakeClock as ExistingClock

    assert FakeClock is ExistingClock, "测试设施必须复用既有 Clock 替身，不新增第二份"


def test_服务启动器装配同一时钟与闸门并复用账本协议替身() -> None:
    from shared.ledger import LedgerRepository

    clock = FakeClock()
    gates = StageGates()
    harness = launch_service(clock=clock, gates=gates)

    assert harness.clock is clock
    assert harness.onebot.gates is gates, "模拟 OneBot 必须与夹具共用同一闸门"
    assert harness.ledger.gates is gates, "账本替身必须与夹具共用同一闸门"
    assert harness.onebot.endpoint == IN_MEMORY_ENDPOINT
    assert isinstance(harness.ledger.delegate, LedgerRepository), (
        "账本替身必须实现 2.2 公布的账本仓储协议"
    )
