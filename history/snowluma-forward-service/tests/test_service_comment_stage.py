"""5.6 评论授权与双端最短间隔检查（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D4/D8 与 tasks 5.6：

- 源未确认落账、空评论、无挑战、错误尝试和权限失效均不能发送；
- 源回执后不足 0.5 秒拒绝且**不排队定时发送**，满足间隔且授权时至多一次评论；
- 客户端未提交挑战或挑战到期时登记跳过，服务器不自主补发。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from shared.result import COMMENT_DELAY_SECONDS
from tests.service_support import (
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    STAGE_PLATFORM_COMMIT,
    TARGET_GROUP_ID,
    AuthorizedService,
    authorize_session,
    error_of,
    launch_authorized_service,
    result_of,
    store_source_message,
)

COMMENT_TEXT = "顺手评论：这段是普通文字，不是命令"


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    return harness


def _prepare_and_send_source(
    harness: AuthorizedService, session_id: str, *, comment: str = COMMENT_TEXT
) -> dict[str, Any]:
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


def _comment_call(
    harness: AuthorizedService,
    delivery: dict[str, Any],
    session_id: str,
    *,
    generation: int = 1,
    challenge: str = "",
    attempt_id: str = "",
    stage: str = "comment",
) -> Any:
    return harness.execute(
        str(delivery["delivery_id"]),
        stage=stage,
        attempt_id=attempt_id or str(delivery["attempt_id"]),
        session_id=str(session_id),
        generation=generation,
        challenge=challenge or str(delivery.get("comment_challenge", "")),
    )


def test_源回执后不足半秒拒绝且不排队定时发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _prepare_and_send_source(harness, str(session_id))
    sends_before = harness.onebot.send_count

    too_early = _comment_call(harness, delivery, str(session_id))
    assert too_early.status == 409, too_early.payload
    assert error_of(too_early)[0] == "comment_too_early"
    assert harness.onebot.send_count == sends_before, "过早提交不得发送，也不得排队"

    harness.clock.advance(COMMENT_DELAY_SECONDS - 0.01)
    still_early = _comment_call(harness, delivery, str(session_id))
    assert still_early.status == 409, still_early.payload
    assert harness.onebot.send_count == sends_before

    harness.clock.advance(0.01)
    accepted = result_of(_comment_call(harness, delivery, str(session_id)))

    assert accepted["comment_status"] == "sent", accepted
    assert accepted["comment_message_id"], accepted
    assert harness.onebot.send_count == sends_before + 1, "满足间隔时恰好一次评论发送"
    comment_call = harness.onebot.send_calls[-1]
    assert comment_call.target == TARGET_GROUP_ID
    assert comment_call.detail["message"] == [{"type": "text", "data": {"text": COMMENT_TEXT}}], (
        "评论原文只成为单个普通文字段"
    )


def test_评论结果落账且不覆盖源目标ID(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _prepare_and_send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    accepted = result_of(_comment_call(harness, delivery, str(session_id)))

    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(delivery["delivery_id"])))
    assert snapshot is not None
    assert snapshot.status == "sent"
    assert snapshot.target_message_id == delivery["target_message_id"]
    assert snapshot.comment_status == "sent"
    assert snapshot.comment_message_id == accepted["comment_message_id"]
    assert snapshot.target_message_id != snapshot.comment_message_id


def test_源未确认落账或没有挑战时评论不能发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment=COMMENT_TEXT))
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    no_source = harness.execute(
        str(prepared["delivery_id"]),
        stage="comment",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge="a-冒充的评论挑战",
    )

    assert no_source.status == 409, no_source.payload
    assert error_of(no_source)[0] == "source_not_sent"
    assert harness.onebot.send_count == 0

    unknown_attempt = _comment_call(
        harness,
        {"delivery_id": prepared["delivery_id"], "attempt_id": "a-不存在", "comment_challenge": "a-任意"},
        str(session_id),
    )
    assert unknown_attempt.status == 404, unknown_attempt.payload
    assert error_of(unknown_attempt)[0] == "not_found"
    assert harness.onebot.send_count == 0


def test_源成功但挑战错误或重复消费都不能发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _prepare_and_send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    wrong = _comment_call(harness, delivery, str(session_id), challenge="a-错误挑战")
    assert wrong.status == 409, wrong.payload
    assert error_of(wrong)[0] == "authorization_unknown"
    assert harness.onebot.send_count == 1

    accepted = result_of(_comment_call(harness, delivery, str(session_id)))
    assert accepted["comment_status"] == "sent"
    assert harness.onebot.send_count == 2

    repeat = result_of(_comment_call(harness, delivery, str(session_id)))
    assert repeat["comment_status"] == "sent", "重复执行只返回原评论结果"
    assert repeat["comment_message_id"] == accepted["comment_message_id"]
    assert harness.onebot.send_count == 2, "同一尝试至多一次评论发送"


def test_评论挑战到期后提交登记跳过且不自主补发(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _prepare_and_send_source(harness, str(session_id))

    for _ in range(3):
        harness.clock.advance(10.0)
        assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(0.1)

    expired = _comment_call(harness, delivery, str(session_id))

    assert expired.status == 409, expired.payload
    assert error_of(expired)[0] == "expired_authorization"
    assert harness.onebot.send_count == 1
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(delivery["delivery_id"])))
    assert snapshot is not None and snapshot.comment_status == "skipped", (
        "挑战到期必须登记跳过，不形成永久待发送队列"
    )


def test_权限代次前进后旧评论挑战不能发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _prepare_and_send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    bumped = result_of(
        harness.update_policy(str(session_id), generation=2, target_groups=(TARGET_GROUP_ID,))
    )
    assert bumped["policy_generation"] == 2

    stale = _comment_call(harness, delivery, str(session_id), generation=2)

    assert stale.status == 409, stale.payload
    assert error_of(stale)[0] in ("authorization_mismatch", "stale_policy")
    assert harness.onebot.send_count == 1, "权限版本前进后旧挑战一律不得发送"


def test_缺省或空白评论不进入评论阶段(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="   "))
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

    assert prepared["comment_status"] == "not_requested", "空白评论不登记为待执行"
    assert executed["status"] == "sent"
    assert executed["comment_challenge"] == "", "空评论不得签发评论挑战"
    harness.clock.advance(COMMENT_DELAY_SECONDS)

    attempted = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="comment",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge="a-任意挑战",
        )
    )

    assert attempted["comment_status"] == "not_requested"
    assert harness.onebot.send_count == 1, "空评论不发送任何评论"


def test_评论阶段可在真实路由上暂停与放行(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery = _prepare_and_send_source(harness, str(session_id))
    harness.clock.advance(COMMENT_DELAY_SECONDS)
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> Any:
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
        for _ in range(300):
            if harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 2:
                break
            await asyncio.sleep(0)
        assert harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= 2, (
            "必须停在评论的平台调用入口"
        )
        assert harness.onebot.send_count == 1, "停住时尚未进入评论平台调用"
        harness.gates.release(STAGE_PLATFORM_COMMIT)
        return await task

    response = asyncio.run(scenario())

    assert result_of(response)["comment_status"] == "sent"
    assert harness.onebot.send_count == 2
    assert harness.onebot.send_calls[-1].detail["message"] == [
        {"type": "text", "data": {"text": COMMENT_TEXT}}
    ]
