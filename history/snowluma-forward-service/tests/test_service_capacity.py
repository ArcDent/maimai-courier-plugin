"""5.8 准备容量、临时材料清理与接口隔离（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D5/D6/D7 与 tasks 5.8：

- 在已公布的请求体、并发与准备容量边界的前一项、临界项、后一项验证行为；
- 超限明确拒绝/繁忙且不启动发送；
- 完成或过期后临时正文、媒体与挑战释放，成功/未知账本不被逐出；
- 不同认证主体不能读取或执行他人投递。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from shared.protocol import delivery_id_for
from shared.result import COMMENT_DELAY_SECONDS
from service.coordinator import MAX_CONCURRENT_EXECUTIONS, MAX_PENDING_PREPARES
from service.http_api import MAX_BODY_BYTES
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SECOND_SUBJECT,
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
from shared.delivery import DeliveryKey


def _service(tmp_path: Path, *, messages: int = 0) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    for index in range(1, messages + 1):
        store_source_message(
            harness.onebot,
            message_id=f"m-{index}",
            group_id=SOURCE_GROUP_ID,
            message=[{"type": "text", "data": {"text": f"第 {index} 条"}}],
        )
    return harness


def _body_of_size(size: int) -> bytes:
    """构造恰好 `size` 字节的合法 JSON 请求体（超长 comment 占位，字段本身合法）。"""

    template = {
        "protocol_version": 1,
        "request_id": "req-body",
        "session_id": "s-anything",
        "policy_generation": 1,
        "source_group_id": SOURCE_GROUP_ID,
        "source_message_id": SOURCE_MESSAGE_ID,
        "target_group_id": TARGET_GROUP_ID,
        "comment": "#",
    }
    base = json.dumps(template, ensure_ascii=False)
    extra = size - len(base.encode("utf-8"))
    assert extra >= 0, (size, len(base))
    return base.replace('"#"', '"' + "#" * (1 + extra) + '"').encode("utf-8")


def test_请求体大小的前一项临界项与后一项(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    authorize_session(harness)

    for size, expected_status in (
        (MAX_BODY_BYTES - 1, 400),
        (MAX_BODY_BYTES, 400),
        (MAX_BODY_BYTES + 1, 413),
    ):
        body = _body_of_size(size)
        assert len(body) == size
        response = harness.call("POST", "/v1/forwards/prepare", body=body)
        assert response.status == expected_status, (size, response.payload)
        if expected_status == 413:
            assert error_of(response)[0] == "body_too_large"
        else:
            assert error_of(response)[0] != "body_too_large", "临界值本身必须被受理"

    assert harness.onebot.send_count == 0


def test_准备容量的前一项临界项与后一项(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    attempts: list[str] = []

    for _ in range(MAX_PENDING_PREPARES):
        prepared = result_of(harness.prepare(str(session_id), generation=1))
        attempts.append(str(prepared["attempt_id"]))

    assert len(set(attempts)) == MAX_PENDING_PREPARES
    assert harness.coordinator.pending_prepares == MAX_PENDING_PREPARES, "临界项必须被受理"

    busy = harness.prepare(str(session_id), generation=1, request_id="req-over-capacity")
    assert busy.status == 429, busy.payload
    assert error_of(busy)[0] == "busy"
    assert harness.onebot.send_count == 0
    assert harness.onebot.query_counts.get("get_msg") == MAX_PENDING_PREPARES, (
        "容量超限必须在查源之前拒绝"
    )


def test_并发准备不会越过容量上限(tmp_path: Path) -> None:
    harness = _service(tmp_path, messages=MAX_PENDING_PREPARES + 1)
    session_id = authorize_session(harness)

    async def scenario() -> list[Any]:
        return await asyncio.gather(
            *(
                harness.call_async(
                    "POST",
                    "/v1/forwards/prepare",
                    payload={
                        "protocol_version": 1,
                        "request_id": f"req-concurrent-{index}",
                        "session_id": session_id,
                        "policy_generation": 1,
                        "source_group_id": SOURCE_GROUP_ID,
                        "source_message_id": f"m-{index}",
                        "target_group_id": TARGET_GROUP_ID,
                    },
                )
                for index in range(1, MAX_PENDING_PREPARES + 2)
            )
        )

    responses = asyncio.run(scenario())
    prepared = [response for response in responses if response.status == 200]
    busy = [response for response in responses if response.status == 429]

    assert len(prepared) == MAX_PENDING_PREPARES, (
        "容量检查与登记之间不得有未占槽的窗口：并发准备不得超过硬上限"
    )
    assert len(busy) == 1, [response.payload for response in responses]
    assert error_of(busy[0])[0] == "busy"
    assert harness.coordinator.pending_prepares == MAX_PENDING_PREPARES
    assert harness.onebot.send_count == 0
    assert harness.onebot.query_counts.get("get_msg") == MAX_PENDING_PREPARES, (
        "超限的准备必须在查源之前被拒绝"
    )


def test_评论终态回收源回执时刻(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="评论"))
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
    assert harness.coordinator.retained_source_marks == 1, "源成功后保留评论最短间隔基准"

    harness.clock.advance(COMMENT_DELAY_SECONDS)
    result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="comment",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(delivery["comment_challenge"]),
        )
    )

    assert harness.coordinator.retained_source_marks == 0, "评论进入终态后必须回收该基准"
    assert harness.onebot.send_count == 2


def test_准备过期后临时材料与挑战释放(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    for _ in range(MAX_PENDING_PREPARES):
        harness.prepare(str(session_id), generation=1)
    assert harness.coordinator.pending_prepares == MAX_PENDING_PREPARES

    # 心跳维持会话，跨过 30 秒准备窗口
    for _ in range(3):
        harness.clock.advance(10.0)
        assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(0.1)

    assert harness.coordinator.pending_prepares == 0, "过期后准备材料与挑战必须释放"
    assert harness.sessions.pending_authorizations == 0
    assert harness.onebot.send_count == 0, "释放不等于发送"
    assert result_of(harness.prepare(str(session_id), generation=1, request_id="req-after-expire"))[
        "status"
    ] == "prepared", "释放后容量必须可再次使用"


def test_成功与未知账本不因容量压力被逐出(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    sent_key = DeliveryKey(
        account_id=BOUND_ACCOUNT_ID,
        source_group_id=SOURCE_GROUP_ID,
        source_message_id="m-sent-1",
        target_group_id=TARGET_GROUP_ID,
    )
    granted = asyncio.run(
        harness.ledger.reserve_source(
            sent_key,
            attempt_id="seed-sent",
            request_id="seed",
            session_id="seed",
            policy_generation=1,
        )
    )
    assert granted.granted is True
    assert asyncio.run(
        harness.ledger.finalize_source(
            sent_key, attempt_id="seed-sent", status="sent", target_message_id="target-sent-1"
        )
    )
    sent_delivery = delivery_id_for(sent_key)

    for _ in range(MAX_PENDING_PREPARES):
        harness.prepare(str(session_id), generation=1)

    blocked = result_of(
        harness.prepare(
            str(session_id), generation=1, source_message_id="m-sent-1", request_id="req-sent"
        )
    )
    assert blocked["status"] == "already_forwarded", "容量压力下既有成功账本仍必须阻断"
    assert blocked["target_message_id"] == "target-sent-1"
    snapshot = asyncio.run(harness.ledger.lookup_delivery(sent_delivery))
    assert snapshot is not None and snapshot.status == "sent"
    assert harness.onebot.send_count == 0


def test_执行并发的临界项与后一项明确繁忙(tmp_path: Path) -> None:
    harness = _service(tmp_path, messages=MAX_CONCURRENT_EXECUTIONS + 1)
    session_id = authorize_session(harness)
    prepared = [
        result_of(
            harness.prepare(
                str(session_id), generation=1, source_message_id=f"m-{index}", request_id=f"req-{index}"
            )
        )
        for index in range(1, MAX_CONCURRENT_EXECUTIONS + 2)
    ]
    harness.gates.pause(STAGE_PLATFORM_COMMIT)

    async def scenario() -> tuple[list[Any], Any]:
        running = [
            asyncio.ensure_future(
                harness.call_async(
                    "POST",
                    f"/v1/forwards/{item['delivery_id']}/source",
                    payload={
                        "protocol_version": 1,
                        "attempt_id": item["attempt_id"],
                        "session_id": session_id,
                        "policy_generation": 1,
                        "challenge": item["challenge"],
                    },
                )
            )
            for item in prepared[:MAX_CONCURRENT_EXECUTIONS]
        ]
        for _ in range(600):
            if harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= MAX_CONCURRENT_EXECUTIONS:
                break
            await asyncio.sleep(0)
        assert harness.gates.arrivals.get(STAGE_PLATFORM_COMMIT, 0) >= MAX_CONCURRENT_EXECUTIONS
        over = await harness.call_async(
            "POST",
            f"/v1/forwards/{prepared[MAX_CONCURRENT_EXECUTIONS]['delivery_id']}/source",
            payload={
                "protocol_version": 1,
                "attempt_id": prepared[MAX_CONCURRENT_EXECUTIONS]["attempt_id"],
                "session_id": session_id,
                "policy_generation": 1,
                "challenge": prepared[MAX_CONCURRENT_EXECUTIONS]["challenge"],
            },
        )
        harness.gates.release(STAGE_PLATFORM_COMMIT)
        return [await task for task in running], over

    completed, over = asyncio.run(scenario())

    assert over.status == 429, over.payload
    assert error_of(over)[0] == "busy"
    assert all(result_of(item)["status"] == "sent" for item in completed)
    assert harness.onebot.send_count == MAX_CONCURRENT_EXECUTIONS, (
        "繁忙拒绝不得启动平台调用，也不得影响已放行的执行"
    )


def test_不同认证主体不能读取或执行他人投递(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    other_token = harness.add_subject(SECOND_SUBJECT)
    session_id = authorize_session(harness)
    prepared = result_of(
        harness.prepare(str(session_id), generation=1, comment="评论")
    )
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
    assert executed["status"] == "sent"

    other_session = result_of(harness.open_session(subject=SECOND_SUBJECT))["session_id"]
    harness.update_policy(str(other_session), generation=1, subject=SECOND_SUBJECT)

    assert other_token
    queried = harness.query(str(prepared["delivery_id"]), subject=SECOND_SUBJECT)
    assert queried.status == 403, queried.payload
    assert error_of(queried)[0] == "delivery_forbidden"

    executed_again = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(prepared["challenge"]),
        subject=SECOND_SUBJECT,
    )
    assert executed_again.status == 403, executed_again.payload

    cancelled = harness.cancel(
        str(prepared["delivery_id"]), attempt_id=str(prepared["attempt_id"]), subject=SECOND_SUBJECT
    )
    assert cancelled.status == 403, cancelled.payload

    assert harness.onebot.send_count == 1, "跨主体访问不得产生任何发送或状态变更"
    assert result_of(harness.query(str(prepared["delivery_id"])))["status"] == "sent"

    # 重启后原尝试不再在内存里：只允许受控绑定主体读取，其它主体仍被拒绝
    harness.restart()
    harness.add_subject(SECOND_SUBJECT)
    other_after_restart = harness.query(str(prepared["delivery_id"]), subject=SECOND_SUBJECT)
    assert other_after_restart.status == 403
    assert error_of(other_after_restart)[0] == "delivery_forbidden"
    bound_session = authorize_session(harness, generation=1)
    assert result_of(harness.query(str(prepared["delivery_id"])))["status"] == "sent"
    assert bound_session
    assert harness.onebot.send_count == 1


def test_准备材料在源执行后释放且不重复使用(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))
    assert harness.coordinator.pending_prepares == 1

    result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )

    assert harness.coordinator.pending_prepares == 0, "执行完成后临时材料立即释放"
    assert harness.sessions.pending_authorizations == 0
    assert harness.onebot.send_count == 1
