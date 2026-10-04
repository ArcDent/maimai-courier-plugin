"""5.4 源阶段授权和唯一平台提交（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D4/D6/D8 与 tasks 5.4：

- 核对尝试、阶段、会话、权限代次与挑战有效性：错阶段、换目标、旧实例、过期挑战都不能发送；
- 占位成功与最终权限复验后才进入**一次**平台 RPC；覆盖 2 秒开始窗口与排队后过期；
- 重复执行只返回原尝试状态，绝不重新发送。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from shared.delivery import DeliveryKey
from shared.protocol import PREPARE_CHALLENGE_SECONDS, STAGE_EXECUTION_WINDOW_SECONDS, delivery_id_for
from tests.service_support import (
    BOUND_ACCOUNT_ID,
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


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    return harness


def _key(source_message_id: str = SOURCE_MESSAGE_ID) -> DeliveryKey:
    return DeliveryKey(
        account_id=BOUND_ACCOUNT_ID,
        source_group_id=SOURCE_GROUP_ID,
        source_message_id=source_message_id,
        target_group_id=TARGET_GROUP_ID,
    )


def _source_payload(prepared: dict[str, Any], session_id: str, *, generation: int = 1) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "attempt_id": prepared["attempt_id"],
        "session_id": session_id,
        "policy_generation": generation,
        "challenge": prepared["challenge"],
    }


async def _run_paused(
    harness: AuthorizedService,
    prepared: dict[str, Any],
    session_id: str,
    *,
    stage: str,
    during: Any,
) -> Any:
    """在指定阶段暂停执行请求，运行 `during()`（可推进时钟/改权限），再放行并返回响应。"""

    harness.gates.pause(stage)
    task = asyncio.ensure_future(
        harness.call_async(
            "POST",
            f"/v1/forwards/{prepared['delivery_id']}/source",
            payload=_source_payload(prepared, session_id),
        )
    )
    for _ in range(400):
        if harness.gates.arrivals.get(stage, 0) >= 1:
            break
        await asyncio.sleep(0)
    assert harness.gates.arrivals.get(stage, 0) >= 1, f"必须真的停在 {stage} 阶段入口"
    await during()
    harness.gates.release(stage)
    return await task


def test_源阶段占位后只提交一次平台调用并落账(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="顺手评论"))

    response = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(prepared["challenge"]),
    )
    result = result_of(response)

    assert result["status"] == "sent", result
    assert result["target_message_id"], result
    assert result["comment_status"] == "pending"
    assert result["comment_challenge"], "源成功后必须为原调用签发一次性评论挑战"
    assert result["comment_challenge_expires_in_seconds"] == PREPARE_CHALLENGE_SECONDS
    assert harness.onebot.send_count == 1, "一次执行只允许一次平台调用"
    assert [call.target for call in harness.onebot.send_calls] == [TARGET_GROUP_ID]

    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(prepared["delivery_id"])))
    assert snapshot is not None
    assert snapshot.status == "sent"
    assert snapshot.target_message_id == result["target_message_id"]
    assert snapshot.comment_status == "pending"


def test_错阶段换目标与旧实例都不能发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    first = result_of(harness.prepare(str(session_id), generation=1))

    wrong_challenge = harness.execute(
        str(first["delivery_id"]),
        stage="source",
        attempt_id=str(first["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge="a-错误的挑战",
    )
    assert wrong_challenge.status == 409, wrong_challenge.payload
    assert error_of(wrong_challenge)[0] == "authorization_unknown"

    # 换目标：把另一个投递的挑战用到本投递上
    store_source_message(
        harness.onebot,
        message_id="m-other",
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "另一条"}}],
    )
    other = result_of(
        harness.prepare(
            str(session_id), generation=1, source_message_id="m-other", request_id="req-other"
        )
    )
    swapped = harness.execute(
        str(first["delivery_id"]),
        stage="source",
        attempt_id=str(first["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(other["challenge"]),
    )
    assert swapped.status == 409, swapped.payload
    assert error_of(swapped)[0] == "authorization_mismatch"

    # 旧实例：同一主体建立新会话后，旧会话的挑战立即失效
    replacement = authorize_session(harness, generation=2)
    stale_instance = harness.execute(
        str(first["delivery_id"]),
        stage="source",
        attempt_id=str(first["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(first["challenge"]),
    )
    assert stale_instance.status == 401, stale_instance.payload
    assert error_of(stale_instance)[0] == "session_invalid"

    # 新实例对新投递仍可正常执行：拒绝只针对失效绑定
    assert harness.heartbeat(replacement).status == 200
    assert harness.onebot.send_count == 0


def test_过期挑战不能发送且不自动重建(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    # 30 秒准备窗口内按心跳纪律维持会话（心跳只续期）
    for _ in range(3):
        harness.clock.advance(10.0)
        assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(0.1)

    response = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(prepared["challenge"]),
    )

    assert response.status == 409, response.payload
    assert error_of(response)[0] == "expired_authorization"
    assert harness.onebot.send_count == 0
    assert asyncio.run(harness.ledger.lookup_delivery(str(prepared["delivery_id"]))) is None, (
        "过期挑战不得重建、不得留下任何发送事实"
    )


def test_排队超过两秒开始窗口即明确未发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    async def during() -> None:
        harness.clock.advance(STAGE_EXECUTION_WINDOW_SECONDS + 1.0)

    response = asyncio.run(
        _run_paused(harness, prepared, str(session_id), stage=STAGE_LEDGER_WRITE, during=during)
    )

    assert response.status == 409, response.payload
    assert error_of(response)[0] == "execution_window_expired"
    assert harness.onebot.send_count == 0, "过窗后不得进入平台调用"
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(prepared["delivery_id"])))
    assert snapshot is not None and snapshot.status == "failed", (
        "占位后过窗必须按「明确未发送」释放，而不是待核实"
    )


def test_两秒窗口临界值仍可开始发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    async def during() -> None:
        harness.clock.advance(STAGE_EXECUTION_WINDOW_SECONDS)

    response = asyncio.run(
        _run_paused(harness, prepared, str(session_id), stage=STAGE_LEDGER_WRITE, during=during)
    )

    assert result_of(response)["status"] == "sent"
    assert harness.onebot.send_count == 1


def test_占位后进入平台调用前仍复验权限与窗口(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    async def during() -> None:
        # 排队期间权限收缩：目标群被移除（新代次），已在队列里的旧授权不得再发送
        revoked = await harness.call_async(
            "PUT",
            f"/v1/sessions/{session_id}/policy",
            payload={
                "protocol_version": 1,
                "policy_generation": 2,
                "enabled": True,
                "source_groups": [SOURCE_GROUP_ID],
                "target_groups": ["20099"],
            },
        )
        assert revoked.status == 200, revoked.payload

    response = asyncio.run(
        _run_paused(harness, prepared, str(session_id), stage=STAGE_LEDGER_WRITE, during=during)
    )

    assert response.status == 409, response.payload
    assert error_of(response)[0] == "stale_policy", (
        "排队期间权限代次已前进：旧代次授权必须在占位后、进入平台 RPC 前被复验拒绝"
    )
    assert harness.onebot.send_count == 0
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(prepared["delivery_id"])))
    assert snapshot is not None and snapshot.status == "failed", (
        "复验失败必须按「明确未发送」释放占位，不得降级为待核实"
    )


def test_占位释放落盘失败时如实说明为待核实(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))
    harness.gated_ledger.fail_next("finalize_source")

    async def during() -> None:
        harness.clock.advance(STAGE_EXECUTION_WINDOW_SECONDS + 1.0)

    response = asyncio.run(
        _run_paused(harness, prepared, str(session_id), stage=STAGE_LEDGER_WRITE, during=during)
    )

    assert response.status == 409, response.payload
    error, reason = error_of(response)
    assert error == "execution_window_expired"
    assert "未能确认落盘" in reason and "待核实" in reason, reason
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(prepared["delivery_id"])))
    assert snapshot is not None and snapshot.status == "sending", (
        "释放未落盘：账本仍停在 sending，重启后按待核实处理（不伪称已释放）"
    )
    assert harness.onebot.send_count == 0, "释放失败也不得进入平台调用"


def test_源发送期间会话被替换后不再签发评论挑战(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="评论"))

    async def during() -> None:
        replacement = await harness.call_async(
            "POST",
            "/v1/sessions",
            payload={"protocol_version": 1, "client_identity": harness.client_identity},
        )
        assert replacement.status == 200, replacement.payload

    response = asyncio.run(
        _run_paused(harness, prepared, str(session_id), stage=STAGE_PLATFORM_COMMIT, during=during)
    )

    result = result_of(response)
    assert result["status"] == "sent", "已开始的平台调用照常按真实回执登记"
    assert result["comment_challenge"] == "", "会话已被替换：不得签发绑定死会话的评论挑战"
    assert result["comment_status"] == "pending", "未提交评论仍以账本事实为准"

    queried = result_of(harness.query(str(prepared["delivery_id"])))
    assert queried["comment_challenge"] == ""
    assert queried["comment_status"] == "skipped", "失去会话的未消费评论授权登记为跳过"
    assert harness.onebot.send_count == 1


def test_重复执行只返回原尝试状态且不再次发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="评论"))

    first = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    assert first["status"] == "sent"

    repeat = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )

    assert repeat["status"] == "sent"
    assert repeat["attempt_id"] == first["attempt_id"]
    assert repeat["target_message_id"] == first["target_message_id"]
    assert repeat["comment_challenge"] == first["comment_challenge"], (
        "重复执行只返回原尝试状态与已签发的挑战，不重新生成"
    )
    assert harness.onebot.send_count == 1, "重复执行不得再次发送"


def test_错阶段复用授权不产生第二次发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="评论"))
    delivered = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    assert delivered["comment_challenge"], "源成功后才有评论挑战"

    # 把评论挑战拿到源阶段复用：绝不产生第二次平台调用
    swapped = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(delivered["comment_challenge"]),
    )

    assert swapped.status in (200, 409), swapped.payload
    assert harness.onebot.send_count == 1, "错阶段复用授权不得发送"
    if swapped.status == 200:
        assert result_of(swapped)["comment_challenge"] == delivered["comment_challenge"], (
            "重复执行只返回原尝试状态与已签发的挑战"
        )


def test_缺少准备或尝试不存在时拒绝且零发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)

    unknown = harness.execute(
        delivery_id_for(_key()),
        stage="source",
        attempt_id="a-不存在",
        session_id=str(session_id),
        generation=1,
        challenge="a-任意",
    )

    assert unknown.status == 404, unknown.payload
    assert error_of(unknown)[0] == "not_found"
    assert harness.onebot.send_count == 0
