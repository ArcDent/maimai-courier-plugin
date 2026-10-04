"""第 7.2 组验收：网络、落盘与授权竞态矩阵（真实服务链 + 阶段闸门 + 独立账本查询）。

共同 seam：真实插件工具 → 真实客户端 → 真实服务（真实路由/协调器/账本）→ 模拟
OneBot；阶段控制用 2.6 的 `StageGates`（准备/平台提交/回执/落账/评论）与账本替身故障
注入，结果用**独立连接**读取投递表。插件↔服务端的 HTTP 请求走**内存传输**
（`ServiceAppTransport`，不经真实套接字）；真实子进程 + 真实回环套接字的跨进程覆盖
见 `tests/test_acceptance_processes.py`。每条注入都记录：输入、源/评论发送次数、权威账本
行与工具结果（`tests.acceptance_support.ScenarioReport`），并把「确定未发」与「可能
已发」分开断言。

覆盖：准备、源提交、源回执、源落账、评论等待、评论提交、评论落账七处逐一注入；
新请求 ID、重复授权、过期挑战、迟到撤销不重复发送；受理/取消请求发出/任务退出都不
冒充平台终态。全部为本地合成消息，不连接真实群。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from delivery_ledger import STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_UNKNOWN

from tests.acceptance_support import (
    assert_report_consistent,
    delivery_query_count,
    scenario_report,
)
from tests.client_support import config_with, make_client_service
from tests.service_support import (
    STAGE_PLATFORM_COMMIT,
    STAGE_RECEIPT,
    authorize_session,
    error_of,
    launch_authorized_service,
    result_of,
    state_ledger_rows,
    store_source_message,
)
from shared.protocol import PREPARE_CHALLENGE_SECONDS
from tests.support import (
    apply_config_update,
    issue_source_call,
    run_bound_forward,
    text_segment,
    wait_until,
)

SOURCE_TEXT = "竞态矩阵的源消息"


def _fixture(tmp_path: Path) -> Any:
    """标准夹具：一条可转发的文字源消息（白名单与账本绑定齐备）。"""

    return make_client_service(tmp_path, raw_message=[text_segment(SOURCE_TEXT)])


def _rows(fixture: Any, message_id: str = "m-source-1") -> tuple[tuple[str, str, str], ...]:
    return tuple(state_ledger_rows(fixture.service.state_dir, message_id=message_id))


def _report(fixture: Any, *, label: str, tool: Any, input: Any = None) -> Any:
    """收报告并做自洽检查（发送次数必须能被账本与工具状态解释）。"""

    report = scenario_report(fixture, label=label, tool=tool, input=input, record=fixture.record())
    assert_report_consistent(report)
    return report


def _service_with_source(tmp_path: Path, *, message_id: str = "m-source-1") -> Any:
    """服务端夹具 + 一条可查询的合成源消息（不含客户端，驱动真实 HTTP 路由）。"""

    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=message_id,
        group_id="10001",
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )
    return harness


# --------------------------------------------------------------------------- 准备阶段


def test_准备阶段的不可达网关故障都零发送(tmp_path: Path) -> None:
    """准备处注入：服务不可达、网关源查询失败、网关断线——全部确定未发。"""

    cases: list[tuple[str, Any]] = [
        ("服务不可达", lambda fixture: fixture.transport.fail_paths.append("/prepare")),
        (
            "网关源查询不可用",
            lambda fixture: fixture.onebot.fail_next(
                "get_msg", {"status": "failed", "retcode": 100, "message": "message not found"}
            ),
        ),
        ("网关断线", lambda fixture: fixture.onebot.disconnect()),
    ]
    seen: list[dict[str, Any]] = []
    for index, (label, inject) in enumerate(cases):
        fixture = _fixture(tmp_path / f"prepare-{index}")
        inject(fixture)

        result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
        report = _report(fixture, label=f"准备·{label}", tool=result, input=SOURCE_TEXT)

        assert str(result["status"]) == "rejected", report.summary()
        assert report.source_send_count == 0 and report.comment_send_count == 0, report.summary()
        assert report.ledger_rows == (), ("准备失败不得留下投递事实", report.summary())
        assert fixture.record() is None, report.summary()
        assert fixture.onebot.send_count == 0, report.summary()
        assert fixture.transport.count("/source") == 0, ("不得提交源阶段授权", report.summary())
        assert fixture.old_send_calls() == [], report.summary()
        seen.append(report.as_dict())
        fixture.onebot.reconnect()
        fixture.close()
    assert len(seen) == 3


# --------------------------------------------------------------------------- 源提交阶段


def test_源提交阶段取消不冒充平台终态且占位先于发送(tmp_path: Path) -> None:
    """停在平台提交入口时取消调用：占位已落盘、零发送；放行后平台调用仍完成一次。"""

    fixture = _fixture(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")
    fixture.onebot.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> tuple[tuple[tuple[str, str, str], ...], dict[str, Any]]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(
            lambda: fixture.onebot.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) == 1
        )
        placeholder = _rows(fixture)
        assert fixture.onebot.send_count == 0, "停在平台提交入口时不得计一次发送"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - 取消必须在平台入口生效
            raise AssertionError("停在平台入口的任务应可被取消")
        fixture.onebot.gates.release(STAGE_PLATFORM_COMMIT)
        await wait_until(lambda: fixture.onebot.send_count == 1)
        for _ in range(200):
            if _rows(fixture) and _rows(fixture)[0][1] == STATUS_SENT:
                break
            await asyncio.sleep(0)
        return placeholder, await fixture.record_async()

    placeholder, record = asyncio.run(scenario())
    report = _report(fixture, label="源提交·任务取消", tool={"status": "cancelled"}, input=SOURCE_TEXT)

    assert len(placeholder) == 1 and placeholder[0] == ("m-source-1", STATUS_SENDING, ""), (
        "进入平台调用前必须先留下发送中占位",
        placeholder,
    )
    assert report.source_send_count == 1, ("客户端断连不取消已开始的平台调用", report.summary())
    assert record is not None and record["status"] == STATUS_SENT, (
        "已完成平台调用的结果必须落账",
        record,
    )

    # 任务退出不冒充平台终态：新工具调用按账本事实阻断，零新增发送。
    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    assert str(repeat["status"]) == "already_forwarded", repeat
    assert fixture.onebot.send_count == 1, "重复调用不得再发送"
    fixture.close()


def test_源提交前网关断线按确定未发且可显式重试(tmp_path: Path) -> None:
    """源提交时账号复验发现网关不可用：确定未发（failed），重连后新调用可重试。"""

    harness = _service_with_source(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    harness.onebot.disconnect()

    outcome = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    rows = state_ledger_rows(harness.state_dir, message_id="m-source-1")

    assert outcome["status"] == STATUS_FAILED, ("确定未发必须按 failed 报告", outcome)
    assert "本次未发送任何消息" in outcome["reason"], outcome
    assert harness.onebot.send_count == 0, "账号不可用不得进入平台调用"
    assert rows and rows[0][1] == STATUS_FAILED, ("确定未发必须落 failed 供显式重试", rows)

    # 重连后重新准备并发送：旧 failed 允许新显式尝试。
    harness.onebot.reconnect()
    retried = result_of(harness.prepare(session_id, generation=1, request_id="req-retry"))
    assert retried["status"] == "prepared", retried
    executed = result_of(
        harness.execute(
            str(retried["delivery_id"]),
            stage="source",
            attempt_id=str(retried["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(retried["challenge"]),
        )
    )
    assert executed["status"] == STATUS_SENT and harness.onebot.send_count == 1, executed
    harness.close()


# --------------------------------------------------------------------------- 源回执阶段


def test_源回执阶段模糊失败按可能已发待核实(tmp_path: Path) -> None:
    """平台返回未成功回执（可能已产生副作用）：一次发送、账本待核实、不得重试。"""

    fixture = _fixture(tmp_path)
    fixture.onebot.fail_next(
        "send_msg", {"status": "failed", "retcode": 79, "message": "send rejected"}
    )

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", comment="不该发出的评论")
    report = _report(fixture, label="源回执·模糊失败", tool=result, input=SOURCE_TEXT)

    assert str(result["status"]) == STATUS_UNKNOWN, report.summary()
    assert result["sent"] is False, report.summary()
    assert report.source_send_count == 1, ("模糊失败代表已经尝试过一次平台动作", report.summary())
    assert report.comment_send_count == 0, ("源未确认成功不得进入评论段", report.summary())
    assert any(row[1] == STATUS_UNKNOWN for row in report.ledger_rows), report.summary()
    assert fixture.transport.count("/comment") == 0, report.summary()

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", comment="不补发")
    assert str(repeat["status"]) in (STATUS_SENDING, STATUS_UNKNOWN), repeat
    assert fixture.onebot.send_count == 1, "待核实记录必须继续阻断重发"
    fixture.close()


def test_源回执阶段响应丢失可经只读查询恢复且不重复提交(tmp_path: Path) -> None:
    """执行响应丢失但服务端已完成：只查询同一投递恢复成功，绝不再次提交发送。"""

    fixture = _fixture(tmp_path)
    fixture.transport.drop_response_paths.append("/source")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    report = _report(fixture, label="源回执·响应丢失可恢复", tool=result, input=SOURCE_TEXT)

    assert str(result["status"]) == STATUS_SENT, report.summary()
    assert report.source_send_count == 1, report.summary()
    assert fixture.transport.count("/source") == 1, ("不得再次提交发送探测", fixture.transport.paths())
    assert delivery_query_count(fixture.transport) == 1, "只允许一次同一投递的只读查询"
    assert report.ledger_rows and report.ledger_rows[0][1] == STATUS_SENT, report.summary()
    fixture.close()


def test_源回执阶段响应丢失且查询不可达按待核实不重发(tmp_path: Path) -> None:
    """响应丢失 + 查询不可达：报告待核实；账本已成功的事实由后续查询确认。"""

    fixture = _fixture(tmp_path)
    fixture.transport.drop_response_paths.append("/source")
    delivery_id = fixture.delivery_id()
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1")

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        # 服务端已记录一次平台发送、客户端响应丢失后，把只读查询也注入为不可达。
        await wait_until(lambda: fixture.onebot.gates.arrivals.get(STAGE_RECEIPT, 0) >= 1)
        fixture.transport.fail_paths.append(delivery_id)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())
    fixture.transport.fail_paths.clear()
    record = fixture.record()
    report = scenario_report(
        fixture, label="源回执·丢失且查询不可达", tool=result, input=SOURCE_TEXT, record=record
    )
    assert_report_consistent(report)

    assert str(result["status"]) == STATUS_UNKNOWN, report.summary()
    assert result["sent"] is False, ("查询不可达时不得声称已知发送成功", report.summary())
    assert report.source_send_count == 1, report.summary()
    assert record is not None and record["status"] == STATUS_SENT, (
        "服务端真实结果必须落账，供事后只读核实",
        record,
    )
    assert fixture.transport.count("/source") == 1, "不得再次提交发送探测"

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    assert str(repeat["status"]) == "already_forwarded", repeat
    assert fixture.onebot.send_count == 1
    fixture.close()


# --------------------------------------------------------------------------- 源落账阶段


def test_源落账失败保留已知回执且重启后归并为待核实(tmp_path: Path) -> None:
    """平台已成功、终态未落盘：报告已知回执但按待核实；重启恢复为未知并继续阻断。"""

    fixture = _fixture(tmp_path)
    fixture.service.gated_ledger.fail_next("finalize_source")

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    before = _rows(fixture)
    report = _report(fixture, label="源落账·落盘失败", tool=result, input=SOURCE_TEXT)

    assert str(result["status"]) == STATUS_UNKNOWN, report.summary()
    assert result["sent"] is True and result["target_message_id"], (
        "平台已知回执必须保留用于报告",
        report.summary(),
    )
    assert result["stage"] == "source_persist", report.summary()
    assert report.source_send_count == 1, report.summary()
    assert len(before) == 1 and before[0] == ("m-source-1", STATUS_SENDING, ""), (
        "落盘失败时在途占位必须保留",
        before,
    )

    # 重启恢复：在途占位归并为待核实，仍阻断重发（不自动续发）。
    fixture.restart_service()
    asyncio.run(fixture.service.ledger.recover_interrupted())
    after = _rows(fixture)
    assert after and after[0][1] == STATUS_UNKNOWN, ("重启恢复必须归并为待核实", after)

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    assert str(repeat["status"]) in (STATUS_SENDING, STATUS_UNKNOWN), repeat
    assert fixture.onebot.send_count == 1, "归并为待核实后仍不得重发"
    fixture.close()


# --------------------------------------------------------------------------- 评论等待阶段


def test_评论等待期间取消与权限撤销都不补发评论(tmp_path: Path) -> None:
    """评论等待处注入：调用取消、总开关关闭——源已投递保留，评论零发送且不补发。"""

    cases: list[tuple[str, str]] = [("等待中取消", "cancel"), ("等待中撤销权限", "revoke")]
    for index, (label, mode) in enumerate(cases):
        fixture = make_client_service(
            tmp_path / f"comment-wait-{index}",
            raw_message=[text_segment(SOURCE_TEXT)],
        )
        arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="不该发出的评论")

        async def scenario(mode: str = mode) -> dict[str, Any]:
            task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
            await wait_until(lambda: fixture.clock.sleeps == [0.5])
            assert fixture.onebot.send_count == 1, "等待期间不得提前发送评论"
            if mode == "cancel":
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    return {"status": "cancelled"}
                raise AssertionError("等待中的任务应可被取消")
            await apply_config_update(
                fixture.plugin, config_with(fixture.plugin, plugin={"enabled": False})
            )
            fixture.clock.advance(0.5)
            return await asyncio.wait_for(task, timeout=5)

        result = asyncio.run(scenario())
        record = fixture.record()
        report = scenario_report(
            fixture, label=f"评论等待·{label}", tool=result, input=SOURCE_TEXT, record=record
        )
        assert_report_consistent(report)

        assert report.source_send_count == 1, report.summary()
        assert report.comment_send_count == 0, report.summary()
        assert record is not None and record["status"] == STATUS_SENT, (
            "源投递事实不得因评论未完成而回滚",
            report.summary(),
        )
        assert (result.get("comment_status") or "") in ("", STATUS_FAILED), report.summary()
        assert fixture.transport.count("/comment") == 0, (
            "取消/撤销后不得提交评论授权",
            fixture.transport.paths(),
        )

        # 撤销权限的用例先恢复开关：重复调用仍只返回既有事实（不重发、不补评论）。
        if mode == "revoke":
            asyncio.run(
                apply_config_update(
                    fixture.plugin, config_with(fixture.plugin, plugin={"enabled": True})
                )
            )
        repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", comment="不补发")
        assert str(repeat["status"]) == "already_forwarded", repeat
        assert fixture.onebot.send_count == 1, "重复调用不得补发源或评论"
        fixture.close()


# --------------------------------------------------------------------------- 评论提交阶段


def test_评论挑战过期登记跳过且不重复发送(tmp_path: Path) -> None:
    """评论提交处注入：挑战过期登记跳过，评论零发送、源投递保留、重复调用不补发。"""

    fixture = _fixture(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="过期评论")

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.clock.sleeps == [0.5])
        assert fixture.onebot.send_count == 1, "等待期间不得提前发送评论"
        fixture.clock.advance(PREPARE_CHALLENGE_SECONDS)  # 评论挑战过期（30 秒）
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())
    record = fixture.record()
    report = scenario_report(
        fixture, label="评论提交·挑战过期", tool=result, input=SOURCE_TEXT, record=record
    )
    assert_report_consistent(report)

    assert result["sent"] is True and result["target_message_id"], report.summary()
    assert str(result["status"]) == "partial", report.summary()
    assert str(result["comment_status"]) == STATUS_FAILED, ("过期评论必须保持未发送", report.summary())
    assert report.source_send_count == 1 and report.comment_send_count == 0, report.summary()
    assert record is not None and record["comment_status"] in ("skipped", STATUS_FAILED), report.summary()

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", comment="重放也不补发")
    assert str(repeat["status"]) == "already_forwarded", repeat
    assert fixture.onebot.send_count == 1, "评论挑战过期后不得重发源消息或补发评论"
    fixture.close()


def test_评论提交阶段网关断线按确定未发且保留源投递(tmp_path: Path) -> None:
    """评论提交时网关断线：评论明确未发（源投递保留），重复调用不补发。"""

    fixture = _fixture(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="断线评论")

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.clock.sleeps == [0.5])
        fixture.onebot.disconnect()
        fixture.clock.advance(0.5)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())
    record = fixture.record()
    report = scenario_report(
        fixture, label="评论提交·网关断线", tool=result, input=SOURCE_TEXT, record=record
    )
    assert_report_consistent(report)

    assert result["sent"] is True, ("源已投递事实必须保留", report.summary())
    assert str(result["status"]) == "partial", report.summary()
    assert str(result["comment_status"]) == STATUS_FAILED, ("评论明确未发", report.summary())
    assert report.source_send_count == 1 and report.comment_send_count == 0, report.summary()
    assert record is not None and record["comment_status"] in ("failed", "skipped"), report.summary()
    fixture.onebot.reconnect()
    fixture.close()


# --------------------------------------------------------------------------- 评论落账阶段


def test_评论落账失败按未知报告且不自动补发(tmp_path: Path) -> None:
    """评论平台调用已发生但结果未落盘：按待核实报告，保留源投递且不补发。"""

    fixture = _fixture(tmp_path)
    arguments = issue_source_call(fixture.plugin, message_id="m-source-1", comment="结果存不下来")

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(fixture.plugin.forward_message(**arguments))
        await wait_until(lambda: fixture.clock.sleeps == [0.5])
        fixture.service.gated_ledger.fail_next("finalize_comment")
        fixture.clock.advance(0.5)
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())
    record = fixture.record()
    report = scenario_report(
        fixture, label="评论落账·落盘失败", tool=result, input=SOURCE_TEXT, record=record
    )
    assert_report_consistent(report)

    assert result["sent"] is True and result["target_message_id"], report.summary()
    assert str(result["comment_status"]) == STATUS_UNKNOWN, (
        "评论结果未能落盘不得声称结果已知",
        report.summary(),
    )
    assert result["stage"] == "comment_persist", report.summary()
    assert report.source_send_count == 1 and report.comment_send_count == 1, (
        "评论平台调用已发生：可能已发，按待核实",
        report.summary(),
    )
    assert record is not None and record["comment_status"] != STATUS_SENT, report.summary()

    repeat, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", comment="不补发")
    assert str(repeat["status"]) == "already_forwarded", repeat
    assert fixture.onebot.send_count == 2, "重复调用不得补发评论（源 1 次 + 评论 1 次）"
    fixture.close()


# --------------------------------------------------------------------------- 授权竞态


def test_新请求ID重复授权过期挑战与迟传撤销都不重复发送(tmp_path: Path) -> None:
    """授权竞态矩阵：逐项记录请求、发送次数、账本与接口结果，平台发送次数始终不变。"""

    harness = _service_with_source(tmp_path)
    session_id = str(authorize_session(harness))
    cases: list[dict[str, Any]] = []

    # ① 重复授权：同一挑战提交两次，第二次只返回原结果，不再发送。
    prepared = result_of(harness.prepare(session_id, generation=1))
    assert harness.onebot.send_count == 0, "受理准备本身不是平台发送"
    first = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    send_after_first = harness.onebot.send_count
    second = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    cases.append(
        {
            "label": "重复授权",
            "first": first["status"],
            "second": second["status"],
            "sends": (send_after_first, harness.onebot.send_count),
        }
    )
    assert send_after_first == 1 and harness.onebot.send_count == 1, cases[-1]
    assert second["target_message_id"] == first["target_message_id"], cases[-1]

    # ② 新请求 ID 重放同一四字段投递：返回既有状态，不查源、不发送、不重开评论。
    get_msg_before = harness.onebot.query_counts.get("get_msg", 0)
    replay = result_of(
        harness.prepare(session_id, generation=1, request_id="req-brand-new", comment="新评论")
    )
    cases.append(
        {
            "label": "新请求ID",
            "replay_status": replay["status"],
            "get_msg_delta": harness.onebot.query_counts.get("get_msg", 0) - get_msg_before,
            "sends": harness.onebot.send_count,
        }
    )
    assert replay["status"] == "already_forwarded", cases[-1]
    assert harness.onebot.send_count == 1 and cases[-1]["get_msg_delta"] == 0, cases[-1]

    # ③ 过期挑战：准备后推进 30 秒，源阶段提交被拒且零发送。
    store_source_message(
        harness.onebot,
        message_id="m-source-2",
        group_id="10001",
        message=[{"type": "text", "data": {"text": "另一条源消息"}}],
    )
    expired = result_of(harness.prepare(session_id, generation=1, source_message_id="m-source-2"))
    # 推进到挑战过期（30 秒）但保持会话存活：每 5 秒心跳一次（会话失联上限 15 秒）。
    for _ in range(7):
        harness.clock.advance(5.0)
        assert result_of(harness.heartbeat(session_id))["session_id"] == session_id
    expired_outcome = harness.execute(
        str(expired["delivery_id"]),
        stage="source",
        attempt_id=str(expired["attempt_id"]),
        session_id=session_id,
        generation=1,
        challenge=str(expired["challenge"]),
    )
    cases.append(
        {
            "label": "过期挑战",
            "error": error_of(expired_outcome)[0],
            "sends": harness.onebot.send_count,
            "rows": state_ledger_rows(harness.state_dir, message_id="m-source-2"),
        }
    )
    assert harness.onebot.send_count == 1, cases[-1]
    assert cases[-1]["error"] in ("expired_authorization", "execution_window_expired"), cases[-1]

    # ④ 迟到撤销：撤销未消费授权后，旧挑战不能再激活发送（只读返回明确未发）。
    late = result_of(harness.prepare(session_id, generation=1, source_message_id="m-source-2"))
    cancelled = result_of(harness.cancel(str(late["delivery_id"]), attempt_id=str(late["attempt_id"])))
    assert cancelled["status"] == "cancelled", cancelled
    late_outcome = result_of(
        harness.execute(
            str(late["delivery_id"]),
            stage="source",
            attempt_id=str(late["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(late["challenge"]),
        )
    )
    cases.append(
        {
            "label": "迟到撤销",
            "cancel": cancelled["status"],
            "late_status": late_outcome["status"],
            "late_sent": late_outcome["status"] == STATUS_SENT,
            "sends": harness.onebot.send_count,
        }
    )
    assert harness.onebot.send_count == 1, cases[-1]
    assert late_outcome["status"] == STATUS_FAILED, (
        "撤销后的旧挑战只能得到明确未发",
        cases[-1],
    )

    # ⑤ 已完成投递上的撤销：返回已完成，不产生新的发送。
    completed = result_of(harness.cancel(str(prepared["delivery_id"])))
    cases.append(
        {"label": "完成后撤销", "status": completed["status"], "sends": harness.onebot.send_count}
    )
    assert completed["status"] == "completed" and harness.onebot.send_count == 1, cases[-1]

    # 每条竞态都记录了发送次数与结果口径。
    assert [case["label"] for case in cases] == ["重复授权", "新请求ID", "过期挑战", "迟到撤销", "完成后撤销"]
    assert all(case["sends"] is not None for case in cases)
    harness.close()


def test_取消请求在源平台调用已开始时只报在途不冒充终态(tmp_path: Path) -> None:
    """撤销与「开始平台发送」的定序：已进入平台调用时撤销只报在途，平台事实照常落账。"""

    harness = _service_with_source(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    harness.onebot.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> tuple[Any, Any]:
        task = asyncio.ensure_future(
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
        for _ in range(400):
            if harness.onebot.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1:
                break
            await asyncio.sleep(0)
        assert harness.onebot.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1, "必须已进入平台调用"
        cancelled = result_of(
            await harness.call_async(
                "POST",
                f"/v1/forwards/{prepared['delivery_id']}/cancel",
                payload={"protocol_version": 1, "attempt_id": prepared["attempt_id"]},
            )
        )
        harness.onebot.gates.release(STAGE_PLATFORM_COMMIT)
        return cancelled, result_of(await task)

    cancelled, executed = asyncio.run(scenario())

    assert cancelled["status"] == "in_flight", ("已开始的平台调用不得被撤销冒充为终态", cancelled)
    assert "不承诺撤回" in cancelled["reason"], cancelled
    assert executed["status"] == STATUS_SENT and harness.onebot.send_count == 1, executed
    rows = state_ledger_rows(harness.state_dir, message_id="m-source-1")
    assert rows and rows[0][1] == STATUS_SENT, ("平台事实必须照常落账", rows)
    harness.close()


def test_评论阶段重复提交同一挑战只发送一次(tmp_path: Path) -> None:
    """评论授权同样是一次性的：重复提交只返回原结果，评论发送次数不变。"""

    harness = _service_with_source(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1, comment="只发一次的评论"))
    source = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    challenge = str(source["comment_challenge"])
    assert challenge, source
    harness.clock.advance(0.5)
    arguments = {
        "delivery_id": str(prepared["delivery_id"]),
        "stage": "comment",
        "attempt_id": str(prepared["attempt_id"]),
        "session_id": session_id,
        "generation": 1,
        "challenge": challenge,
    }
    first = result_of(harness.execute(**arguments))
    sends_after_first = harness.onebot.send_count
    second = result_of(harness.execute(**arguments))
    rows = state_ledger_rows(harness.state_dir, message_id="m-source-1")

    assert first["comment_status"] == STATUS_SENT and sends_after_first == 2, first
    assert second["comment_status"] == STATUS_SENT and second["comment_message_id"] == first["comment_message_id"], (
        second
    )
    assert harness.onebot.send_count == 2, ("重复评论提交不得再发送", harness.onebot.send_calls)
    assert rows and rows[0][1] == STATUS_SENT, rows
    harness.close()
