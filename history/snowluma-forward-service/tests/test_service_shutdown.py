"""5.9 服务退出与客户端断连后的结果保全（真实路由 + 真实账本 + 注入时钟）。

判据来自 design D6/D7 与 tasks 5.9：

- 客户端断开不使已开始的平台 RPC 放弃结果登记；
- 服务器仅管理自身任务并有界退出，超时日志列出投递/阶段；
- 未确定退出的源或评论在下次启动为未知，服务器不恢复自动发送队列。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from shared.protocol import HEARTBEAT_INTERVAL_SECONDS
from tests.service_support import (
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    STAGE_PLATFORM_COMMIT,
    AuthorizedService,
    authorize_session,
    error_of,
    launch_authorized_service,
    result_of,
    store_source_message,
)


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    return harness


async def _wait_for(predicate: Any, *, ticks: int = 800) -> None:
    """在事件循环里推进直到条件成立（不真实等待）。"""

    for _ in range(ticks):
        if await predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("等待条件超时（服务退出用例）")


async def _start_source(harness: AuthorizedService, prepared: dict[str, Any], session_id: str) -> Any:
    """发起一次源执行请求（返回未完成的 handler 任务）。"""

    return asyncio.ensure_future(
        harness.call_async(
            "POST",
            f"/v1/forwards/{prepared['delivery_id']}/source",
            payload={
                "protocol_version": 1,
                "attempt_id": prepared["attempt_id"],
                "session_id": session_id,
                "policy_generation": 1,
                "challenge": prepared["challenge"],
            },
        )
    )


def test_客户端断开不使已开始的平台调用放弃结果登记(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> Any:
        handler = await _start_source(harness, prepared, session_id)
        await _wait_for(lambda: asyncio.sleep(0, result=harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1))
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        assert harness.onebot.send_count == 0, "断开时仍停在平台调用入口"

        harness.gates.release(STAGE_PLATFORM_COMMIT)

        async def persisted() -> bool:
            snapshot = await harness.ledger.lookup_delivery(str(prepared["delivery_id"]))
            return snapshot is not None and snapshot.status == "sent"

        await _wait_for(persisted)
        return await harness.ledger.lookup_delivery(str(prepared["delivery_id"]))

    snapshot = asyncio.run(scenario())

    assert snapshot is not None
    assert snapshot.status == "sent"
    assert snapshot.target_message_id, "断开后仍必须完成真实回执登记"
    assert harness.onebot.send_count == 1


def test_服务退出有界等待并列出超时任务(tmp_path: Path) -> None:
    from service.coordinator import TASK_EXIT_WAIT_SECONDS, ShutdownReport

    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> tuple[ShutdownReport, Any]:
        unrelated = asyncio.ensure_future(asyncio.sleep(3600))
        handler = await _start_source(harness, prepared, session_id)
        await _wait_for(
            lambda: asyncio.sleep(0, result=harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1)
        )
        shutdown = asyncio.ensure_future(harness.coordinator.shutdown())
        for _ in range(100):
            await asyncio.sleep(0)
        assert shutdown.done() is False, "注入时钟未推进前不得视为已退出"

        harness.clock.advance(TASK_EXIT_WAIT_SECONDS)
        report = await shutdown

        assert unrelated.done() is False, "只管理本协调器的任务，不取消其它任务"
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)
        await asyncio.gather(handler, return_exceptions=True)
        return report, await harness.ledger.lookup_delivery(str(prepared["delivery_id"]))

    report, snapshot = asyncio.run(scenario())

    assert report.waited_seconds >= TASK_EXIT_WAIT_SECONDS
    assert report.leftover, "超时退出必须逐条列出残留任务"
    described = report.leftover[0]
    assert str(prepared["delivery_id"]) in described
    assert str(prepared["attempt_id"]) in described
    assert "阶段 source" in described
    assert harness.coordinator.in_flight == ()
    assert snapshot is not None and snapshot.status == "sending", (
        "未确定结束的源发送保留在途占位，留待下次启动归并为待核实"
    )
    assert harness.onebot.send_count == 0


def test_退出收尾对吞掉取消的任务仍有上界(tmp_path: Path) -> None:
    """平台 RPC 吞掉取消时，退出收尾仍必须有上界：超时如实报告残留，不无限等待。"""

    from service.coordinator import TASK_CANCEL_WAIT_SECONDS, TASK_EXIT_WAIT_SECONDS
    from shared.result import SourceSendResult

    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    release = asyncio.Event()
    entered = asyncio.Event()

    async def stubborn_send(request: Any) -> SourceSendResult:
        del request
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # 模拟平台 RPC 吞掉取消：绝不让退出退化为无界等待
        return SourceSendResult(status="failed", reason="测试替身：未发送任何消息")

    async def scenario() -> Any:
        harness.gateway.send_source = stubborn_send  # type: ignore[method-assign]
        handler = await _start_source(harness, prepared, session_id)
        await _wait_for(lambda: asyncio.sleep(0, result=entered.is_set()))
        shutdown = asyncio.ensure_future(harness.coordinator.shutdown())
        for _ in range(100):
            await asyncio.sleep(0)
        assert shutdown.done() is False, "第一段有界等待未超时前不得退出"

        harness.clock.advance(TASK_EXIT_WAIT_SECONDS)
        for _ in range(100):
            await asyncio.sleep(0)
        assert shutdown.done() is False, "取消残留任务后进入收尾窗口"

        harness.clock.advance(TASK_CANCEL_WAIT_SECONDS)
        for _ in range(200):
            await asyncio.sleep(0)
        assert shutdown.done() is True, "退出收尾不得无限等待吞掉取消的任务"
        report = await shutdown
        assert report.leftover, "超时退出必须如实报告残留"
        assert str(prepared["delivery_id"]) in report.leftover[0]

        release.set()
        await asyncio.gather(handler, return_exceptions=True)
        return report

    report = asyncio.run(scenario())

    assert str(prepared["delivery_id"]) in report.leftover[0]
    assert harness.onebot.send_count == 0, "吞掉取消的发送替身不产生任何真实发送"


def test_重启把未确定退出的在途归并为未知且不自动续发(tmp_path: Path) -> None:
    from service.coordinator import TASK_EXIT_WAIT_SECONDS

    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

    async def abort() -> None:
        handler = await _start_source(harness, prepared, session_id)
        await _wait_for(
            lambda: asyncio.sleep(0, result=harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1)
        )
        shutdown = asyncio.ensure_future(harness.coordinator.shutdown())
        for _ in range(100):
            await asyncio.sleep(0)
        assert shutdown.done() is False, "注入时钟未推进前不得视为已退出"
        harness.clock.advance(TASK_EXIT_WAIT_SECONDS)
        await shutdown
        await asyncio.gather(handler, return_exceptions=True)

    asyncio.run(abort())
    harness.gates.release(STAGE_PLATFORM_COMMIT)
    sends_before = harness.onebot.send_count

    harness.restart()
    recovery = asyncio.run(harness.ledger.recover_interrupted())
    assert recovery.sources_recovered == 1, "在途源发送必须归并为待核实"

    session_id = str(authorize_session(harness, generation=1))
    blocked = result_of(harness.prepare(session_id, generation=1, request_id="req-after-restart"))

    assert blocked["status"] == "unknown", "下次启动后该键仍阻断重发"
    assert blocked["challenge"] == ""
    assert harness.onebot.send_count == sends_before, "服务器不恢复自动发送队列"
    harness.gates.release(STAGE_PLATFORM_COMMIT)


def test_未退出的评论同样在重启后归并为未知(tmp_path: Path) -> None:
    from shared.result import COMMENT_DELAY_SECONDS

    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1, comment="评论"))
    delivery = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    assert delivery["comment_status"] == "pending"
    harness.clock.advance(COMMENT_DELAY_SECONDS)
    sends_before = harness.onebot.send_count

    # 评论在途占位（模拟平台调用未确定结束）：占位后进程退出
    key_snapshot = asyncio.run(harness.ledger.lookup_delivery(str(delivery["delivery_id"])))
    assert key_snapshot is not None
    reserved = asyncio.run(
        harness.ledger.reserve_comment(key_snapshot.key, attempt_id=str(delivery["attempt_id"]))
    )
    assert reserved.granted is True

    recovery = asyncio.run(harness.ledger.recover_interrupted())

    assert recovery.comments_recovered == 1, "在途评论必须归并为待核实"
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(delivery["delivery_id"])))
    assert snapshot is not None and snapshot.comment_status == "unknown"
    assert snapshot.status == "sent" and snapshot.target_message_id, "源已投递事实保留"
    assert harness.onebot.send_count == sends_before, "重启不自动补发评论"


def test_启动CLI在HTTP服务端不可用时如实报告(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """监听失败或缺少网络库：`start` 必须报告接口不可用，且不发送任何消息。"""

    import json
    import socket

    from service.cli import EXIT_UNAVAILABLE, ServiceCommandRunner, main
    from tests.service_support import FakeOneBot

    client = _service(tmp_path)
    identity = asyncio.run(client.ledger.identity())
    client.close()

    # 占住一个回环端口：真实监听必然失败（缺 aiohttp 时也会得到同一「不可用」结论）
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        occupied_port = int(holder.getsockname()[1])

        config = tmp_path / "service.toml"
        config.write_text(
            "\n".join(
                [
                    "[service]",
                    'client_identity = "maibot-plugin"',
                    'account_id = "10000"',
                    f'state_dir = "{client.state_dir}"',
                    f"ledger_id = {json.dumps(identity)}",
                    'listen_host = "127.0.0.1"',
                    f"listen_port = {occupied_port}",
                    'token_env_var = "MC_TEST_SERVICE_TOKEN"',
                    'onebot_endpoint = "ws://127.0.0.1:9/onebot"',
                    'onebot_token_env_var = "MC_TEST_ONEBOT_TOKEN"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MC_TEST_SERVICE_TOKEN", "token-value")
        monkeypatch.setenv("MC_TEST_ONEBOT_TOKEN", "onebot-value")
        onebot = FakeOneBot(account_id=10000)

        code = main(
            ["start", "--config", str(config)],
            commands=ServiceCommandRunner(transport_factory=lambda config, clock: onebot),
        )
    output = capsys.readouterr().err

    assert code == EXIT_UNAVAILABLE, output
    assert "HTTP 服务端不可用" in output, output
    assert "账号" in output and "就绪" in output, output
    assert onebot.send_count == 0, "启动阶段绝不发送任何消息"


def test_心跳间隔常量与服务退出等待常量都是代码常量(tmp_path: Path) -> None:
    from service.coordinator import TASK_EXIT_WAIT_SECONDS

    assert HEARTBEAT_INTERVAL_SECONDS == 5.0
    assert TASK_EXIT_WAIT_SECONDS == 5.0
    assert not isinstance(TASK_EXIT_WAIT_SECONDS, bool)
    assert error_of.__module__.endswith("service_support")
