"""5.7 撤销与注销的确定顺序（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D6 与 tasks 5.7：

- 源与评论分别覆盖「撤销先完成」「平台调用先开始」与迟到请求；
- 撤销先完成则零发送，已开始则返回真实结果或未知，不伪称已取消；
- 注销使未消费授权失效，已保存源回执不被删除；
- 最终检查与发送开始之间没有未复验的排队窗口。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from shared.protocol import delivery_id_for
from shared.result import COMMENT_DELAY_SECONDS
from tests.service_support import (
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    STAGE_LEDGER_WRITE,
    STAGE_PLATFORM_COMMIT,
    TARGET_GROUP_ID,
    AuthorizedService,
    authorize_session,
    error_of,
    launch_authorized_service,
    result_of,
    store_source_message,
)

COMMENT_TEXT = "撤销用例的评论"


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    return harness


def _send_source(harness: AuthorizedService, session_id: str, *, comment: str = COMMENT_TEXT) -> dict[str, Any]:
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment=comment))
    executed = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    assert executed["status"] == "sent", executed
    return {"delivery_id": prepared["delivery_id"], "attempt_id": prepared["attempt_id"], **executed}


def test_撤销先完成则迟到源执行零发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    cancelled = result_of(harness.cancel(str(prepared["delivery_id"]), attempt_id=str(prepared["attempt_id"])))
    assert cancelled["status"] == "cancelled", cancelled

    late = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    assert late["status"] == "failed", "已撤销的尝试只能报告「明确未发送」，不得重启"
    assert "撤销" in str(late["reason"])
    assert harness.onebot.send_count == 0, "撤销先完成必须零发送"

    attempt = asyncio.run(harness.ledger.lookup_attempt(str(prepared["attempt_id"])))
    assert attempt is not None and attempt.source_stage == "cancelled", (
        "未消费的准备必须按「明确未发送」登记为已撤销"
    )
    assert asyncio.run(harness.ledger.lookup_delivery(str(prepared["delivery_id"]))) is None


def test_平台调用先开始则撤销报告在途并保留真实结果(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

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
            if harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1:
                break
            await asyncio.sleep(0)
        assert harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 1, "必须已进入平台调用"
        cancelled = await harness.call_async(
            "POST",
            f"/v1/forwards/{prepared['delivery_id']}/cancel",
            payload={"protocol_version": 1, "attempt_id": prepared["attempt_id"]},
        )
        harness.gates.release(STAGE_PLATFORM_COMMIT)
        return cancelled, await task

    cancelled, executed = asyncio.run(scenario())

    assert result_of(cancelled)["status"] == "in_flight", cancelled.payload
    assert "不承诺撤回" in str(result_of(cancelled)["reason"])
    result = result_of(executed)
    assert result["status"] == "sent", result
    assert harness.onebot.send_count == 1

    afterwards = result_of(
        harness.cancel(str(prepared["delivery_id"]), attempt_id=str(prepared["attempt_id"]))
    )
    assert afterwards["status"] == "completed", "已结束的投递按真实结果报告，不重复撤销"
    assert "sent" in str(afterwards["reason"])
    assert harness.onebot.send_count == 1


def test_撤销后未消费的评论授权失效且源回执保留(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    cancelled = result_of(harness.cancel(str(delivery["delivery_id"]), attempt_id=str(delivery["attempt_id"])))
    assert cancelled["status"] == "completed", cancelled
    assert harness.onebot.send_count == 1, "撤销只处理未消费授权，不产生任何发送"

    late_comment = harness.execute(
        str(delivery["delivery_id"]),
        stage="comment",
        attempt_id=str(delivery["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(delivery["comment_challenge"]),
    )
    late_result = result_of(late_comment)
    assert late_result["comment_status"] == "skipped", "迟到评论请求只返回已撤销的事实"
    assert late_result["status"] == "sent", "源已投递事实不变"
    assert harness.onebot.send_count == 1, "迟到请求不得产生任何发送"

    queried = result_of(harness.query(str(delivery["delivery_id"])))
    assert queried["status"] == "sent", "已保存的源回执不因撤销被删除"
    assert queried["target_message_id"] == delivery["target_message_id"]
    assert queried["comment_status"] == "skipped", "未消费的评论授权撤销后登记跳过"


def test_评论平台调用已开始则撤销不撤回评论(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> tuple[Any, Any]:
        task = asyncio.ensure_future(
            harness.call_async(
                "POST",
                f"/v1/forwards/{delivery['delivery_id']}/comment",
                payload={
                    "protocol_version": 1,
                    "attempt_id": delivery["attempt_id"],
                    "session_id": session_id,
                    "policy_generation": 1,
                    "challenge": delivery["comment_challenge"],
                },
            )
        )
        for _ in range(400):
            if harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 2:
                break
            await asyncio.sleep(0)
        assert harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 2, "必须已进入评论平台调用"
        cancelled = await harness.call_async(
            "POST",
            f"/v1/forwards/{delivery['delivery_id']}/cancel",
            payload={"protocol_version": 1, "attempt_id": delivery["attempt_id"]},
        )
        harness.gates.release(STAGE_PLATFORM_COMMIT)
        return cancelled, await task

    cancelled, executed = asyncio.run(scenario())

    assert result_of(cancelled)["status"] == "in_flight", cancelled.payload
    assert result_of(executed)["comment_status"] == "sent"
    assert harness.onebot.send_count == 2


def test_注销使未消费授权失效且不删除已保存回执(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    delivery = _send_source(harness, session_id)

    closed = harness.deregister(session_id)
    assert result_of(closed)["session_id"] == session_id

    late_comment = harness.execute(
        str(delivery["delivery_id"]),
        stage="comment",
        attempt_id=str(delivery["attempt_id"]),
        session_id=session_id,
        generation=1,
        challenge=str(delivery["comment_challenge"]),
    )
    assert late_comment.status == 401, late_comment.payload
    assert error_of(late_comment)[0] == "session_invalid"
    assert harness.onebot.send_count == 1

    rebuilt = authorize_session(harness, generation=2)
    queried = result_of(harness.query(str(delivery["delivery_id"])))
    assert queried["status"] == "sent", "注销不删除已保存的源回执"
    assert queried["target_message_id"] == delivery["target_message_id"]
    assert queried["comment_status"] == "skipped"

    blocked = result_of(harness.prepare(rebuilt, generation=2, request_id="req-rebuild"))
    assert blocked["status"] == "already_forwarded", "已保存回执继续阻断重发"


def test_撤销未知投递明确不存在且零发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    authorize_session(harness)

    missing = result_of(harness.cancel("d-不存在"))

    assert missing["status"] == "not_found"
    assert harness.onebot.send_count == 0


def test_撤销落盘失败时不伪称已撤销(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    harness.gated_ledger.fail_next("cancel_attempt")
    refused = harness.cancel(str(prepared["delivery_id"]), attempt_id=str(prepared["attempt_id"]))

    error, reason = error_of(refused)
    assert refused.status == 503, refused.payload
    assert error == "ledger_unavailable"
    assert "未能落盘" in reason and "不伪称" in reason, reason
    attempt = asyncio.run(harness.ledger.lookup_attempt(str(prepared["attempt_id"])))
    assert attempt is not None and attempt.source_stage == "prepared", (
        "撤销未落盘：账本仍是可显式重试的未消费准备，不是待核实"
    )
    assert harness.onebot.send_count == 0

    # 授权已经失效：迟到执行不能发送
    late = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(prepared["challenge"]),
    )
    assert late.status == 409, late.payload
    assert error_of(late)[0] == "authorization_unknown"
    assert harness.onebot.send_count == 0

    # 账本状态未变：显式重试撤销可以成功
    retried = result_of(
        harness.cancel(str(prepared["delivery_id"]), attempt_id=str(prepared["attempt_id"]))
    )
    assert retried["status"] == "cancelled", retried
    attempt = asyncio.run(harness.ledger.lookup_attempt(str(prepared["attempt_id"])))
    assert attempt is not None and attempt.source_stage == "cancelled"
    assert harness.onebot.send_count == 0


def test_撤销评论跳过登记落盘失败时如实说明(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    harness.gated_ledger.fail_next("finalize_comment")
    cancelled = result_of(
        harness.cancel(str(delivery["delivery_id"]), attempt_id=str(delivery["attempt_id"]))
    )

    assert cancelled["status"] == "completed", "撤销本身已完成（未消费授权已撤销）"
    assert "未能落盘" in str(cancelled["reason"]), cancelled["reason"]
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(delivery["delivery_id"])))
    assert snapshot is not None and snapshot.comment_status == "pending", (
        "跳过登记未落盘：不伪称评论已跳过，真实状态仍是未提交的 pending"
    )
    assert harness.onebot.send_count == 1, "撤销只处理未消费授权，不产生任何发送"


def test_排队中的撤销等在途定序之后且不产生额外发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))
    harness.gates.pause(STAGE_LEDGER_WRITE)

    async def scenario() -> tuple[Any, Any]:
        execution = asyncio.ensure_future(
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
            if harness.gates.arrivals.get(STAGE_LEDGER_WRITE, 0) >= 1:
                break
            await asyncio.sleep(0)
        assert harness.gates.arrivals.get(STAGE_LEDGER_WRITE, 0) >= 1, "必须停在占位落账之前"
        cancel_task = asyncio.ensure_future(
            harness.call_async(
                "POST",
                f"/v1/forwards/{prepared['delivery_id']}/cancel",
                payload={"protocol_version": 1, "attempt_id": prepared["attempt_id"]},
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
        harness.gates.release(STAGE_LEDGER_WRITE)
        return await cancel_task, await execution

    cancelled, executed = asyncio.run(scenario())

    assert result_of(cancelled)["status"] == "in_flight", (
        "撤销排在在途阶段之后：只能报告在途，不能假装已取消"
    )
    assert result_of(executed)["status"] == "sent"
    assert harness.onebot.send_count == 1


def _unused_key() -> str:
    from shared.delivery import DeliveryKey

    return delivery_id_for(
        DeliveryKey(
            account_id="10000",
            source_group_id=SOURCE_GROUP_ID,
            source_message_id="m-从来不存在",
            target_group_id=TARGET_GROUP_ID,
        )
    )
