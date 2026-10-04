"""3.3 尝试身份与旧请求重放保护（design D7「重复旧尝试只返回旧结果」）。

- 投递身份只由账号+源群+源消息+目标群决定：并发请求、更换请求 ID/评论、客户端
  重连（换会话）都不改变投递 ID；
- 同一投递并发占位至多一个取得发送资格；
- 旧尝试（无论请求 ID 如何重放）只返回原结果，不再次发送；
- 新的显式尝试只在旧结果明确未发（`failed`）时获准；同消息不同目标互不影响。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from service.ledger_store import (
    ATTEMPT_CANCELLED,
    ATTEMPT_PREPARED,
    COMMENT_PENDING,
    ServiceLedger,
)
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    SendStatus,
)
from shared.ledger import AttemptSnapshot, SourceReservation
from shared.protocol import delivery_id_for

from tests.service_support import close_ledger, open_state_ledger, state_ledger_rows
from tests.support import ledger_key


def _prepare(
    ledger: ServiceLedger,
    key: DeliveryKey,
    *,
    attempt_id: str,
    request_id: str = "req-1",
    session_id: str = "sess-1",
    comment: str = "",
) -> AttemptSnapshot | None:
    return asyncio.run(
        ledger.prepare_attempt(
            key,
            attempt_id=attempt_id,
            request_id=request_id,
            session_id=session_id,
            policy_generation=1,
            comment=comment,
        )
    )


def _reserve(
    ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str = "attempt-1"
) -> SourceReservation:
    return asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
        )
    )


def _finalize(
    ledger: ServiceLedger,
    key: DeliveryKey,
    *,
    attempt_id: str = "attempt-1",
    status: SendStatus,
    target_message_id: str = "",
    reason: str = "",
) -> bool:
    return asyncio.run(
        ledger.finalize_source(
            key,
            attempt_id=attempt_id,
            status=status,
            target_message_id=target_message_id,
            reason=reason,
        )
    )


def _attempt(ledger: ServiceLedger, attempt_id: str) -> AttemptSnapshot | None:
    return asyncio.run(ledger.lookup_attempt(attempt_id))


# ------------------------------------------------------------------- 身份不变


def test_并发请求至多一个取得占位且投递身份不变(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _prepare(
            ledger, key, attempt_id="attempt-a", request_id="req-a", comment="评论甲"
        ) is not None
        assert _prepare(
            ledger, key, attempt_id="attempt-b", request_id="req-b", comment="评论乙"
        ) is not None

        async def both() -> list[SourceReservation]:
            return await asyncio.gather(
                ledger.reserve_source(
                    key,
                    attempt_id="attempt-a",
                    request_id="req-a",
                    session_id="sess-a",
                    policy_generation=1,
                ),
                ledger.reserve_source(
                    key,
                    attempt_id="attempt-b",
                    request_id="req-b",
                    session_id="sess-b",
                    policy_generation=2,
                ),
            )

        results = asyncio.run(both())

        granted = [result for result in results if result.granted]
        assert len(granted) == 1, "同一投递并发占位至多一个取得发送资格"
        assert {result.snapshot.delivery_id for result in results} == {delivery_id_for(key)}, (
            "请求 ID、会话与权限代次都不参与投递身份"
        )
        blocked = [result for result in results if not result.granted][0]
        assert blocked.snapshot.status == STATUS_SENDING
    finally:
        close_ledger(ledger)


def test_更换请求ID评论与重连不改变四字段身份(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        first = _prepare(
            ledger, key, attempt_id="attempt-1", request_id="req-1", session_id="sess-1",
            comment="第一次评论",
        )
        assert first is not None and first.delivery_id == delivery_id_for(key)
        assert _reserve(ledger, key, attempt_id="attempt-1").granted is True
        assert _finalize(
            ledger, key, attempt_id="attempt-1", status=STATUS_FAILED, reason="明确未发送"
        ) is True

        second = _prepare(
            ledger, key, attempt_id="attempt-2", request_id="req-2", session_id="sess-2",
            comment="换了的评论",
        )
        assert second is not None
        assert second.delivery_id == first.delivery_id, "换请求 ID/评论/会话不得改变投递身份"
        assert _reserve(ledger, key, attempt_id="attempt-2").granted is True
        assert _finalize(
            ledger,
            key,
            attempt_id="attempt-2",
            status=STATUS_SENT,
            target_message_id="m-target-2",
        ) is True

        snapshot = asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))
        assert snapshot is not None
        assert (snapshot.delivery_id, snapshot.status, snapshot.target_message_id) == (
            first.delivery_id,
            STATUS_SENT,
            "m-target-2",
        )
    finally:
        close_ledger(ledger)


# --------------------------------------------------------------- 重放保护


def test_旧失败尝试重复提交只返回原结果不再次发送(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _reserve(ledger, key, attempt_id="attempt-1").granted is True
        assert _finalize(
            ledger, key, attempt_id="attempt-1", status=STATUS_FAILED, reason="平台调用前未发出"
        ) is True
        before = state_ledger_rows(tmp_path)

        for request_id in ("req-1", "req-replay", "req-again"):
            replay = asyncio.run(
                ledger.reserve_source(
                    key,
                    attempt_id="attempt-1",
                    request_id=request_id,
                    session_id="sess-other",
                    policy_generation=9,
                )
            )
            assert replay.granted is False, "旧尝试重放不得再次发送"
            assert replay.snapshot.status == STATUS_FAILED
            assert replay.snapshot.reason == "平台调用前未发出"

        assert state_ledger_rows(tmp_path) == before, "重放不得改写账本（不得重新占位）"
        attempt = _attempt(ledger, "attempt-1")
        assert attempt is not None
        assert attempt.source_stage == STATUS_FAILED, "尝试保留原执行结果"
    finally:
        close_ledger(ledger)


def test_成功与未知的旧尝试重放同样只返回原结果(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        sent_key = ledger_key(message_id="m-sent")
        assert _reserve(ledger, sent_key, attempt_id="attempt-sent").granted is True
        assert _finalize(
            ledger,
            sent_key,
            attempt_id="attempt-sent",
            status=STATUS_SENT,
            target_message_id="m-target-1",
        ) is True

        unknown_key = ledger_key(message_id="m-unknown")
        assert _reserve(ledger, unknown_key, attempt_id="attempt-unknown").granted is True
        assert _finalize(
            ledger, unknown_key, attempt_id="attempt-unknown", status=STATUS_UNKNOWN, reason="回执丢失"
        ) is True

        sent_replay = _reserve(ledger, sent_key, attempt_id="attempt-sent")
        assert sent_replay.granted is False
        assert (sent_replay.snapshot.status, sent_replay.snapshot.target_message_id) == (
            STATUS_SENT,
            "m-target-1",
        )

        unknown_replay = _reserve(ledger, unknown_key, attempt_id="attempt-unknown")
        assert unknown_replay.granted is False
        assert unknown_replay.snapshot.status == STATUS_UNKNOWN
    finally:
        close_ledger(ledger)


def test_新显式尝试只在旧结果明确未发时获准(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _reserve(ledger, key, attempt_id="attempt-1").granted is True
        assert _finalize(
            ledger, key, attempt_id="attempt-1", status=STATUS_UNKNOWN, reason="模糊失败"
        ) is True

        assert _reserve(ledger, key, attempt_id="attempt-2").granted is False, (
            "待核实结果不得被新尝试绕过"
        )
        assert _attempt(ledger, "attempt-2") is None, "被拒绝的尝试不得凭空建立"

        retry_key = ledger_key(message_id="m-retry")
        assert _reserve(ledger, retry_key, attempt_id="attempt-r1").granted is True
        assert _finalize(
            ledger, retry_key, attempt_id="attempt-r1", status=STATUS_FAILED, reason="确定未发出"
        ) is True
        assert _reserve(ledger, retry_key, attempt_id="attempt-r2").granted is True, (
            "明确未发的失败允许新的显式尝试"
        )
    finally:
        close_ledger(ledger)


def test_同消息不同目标可独立进行(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        first = ledger_key(target_group_id="20002")
        second = ledger_key(target_group_id="20003")

        assert _reserve(ledger, first, attempt_id="attempt-first").granted is True
        assert _finalize(
            ledger,
            first,
            attempt_id="attempt-first",
            status=STATUS_SENT,
            target_message_id="m-target-first",
        ) is True
        assert _reserve(ledger, second, attempt_id="attempt-second").granted is True, (
            "同消息不同目标是不同投递身份，不因已有成功记录被阻断"
        )
        assert delivery_id_for(first) != delivery_id_for(second)
        assert state_ledger_rows(tmp_path) == [
            ("m-source-1", STATUS_SENT, "m-target-first"),
            ("m-source-1", STATUS_SENDING, ""),
        ]
    finally:
        close_ledger(ledger)


# ------------------------------------------------------------- 准备与消费


def test_准备创建的尝试在消费前不产生发送事实(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        prepared = _prepare(
            ledger, key, attempt_id="attempt-1", request_id="req-1", comment="  附带评论  "
        )

        assert prepared is not None
        assert prepared.source_stage == ATTEMPT_PREPARED
        assert prepared.comment_stage == COMMENT_PENDING
        assert state_ledger_rows(tmp_path) == [], "准备不得产生任何发送事实（没有投递记录）"

        assert _reserve(ledger, key, attempt_id="attempt-1").granted is True
        snapshot = asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))
        assert snapshot is not None
        assert (snapshot.status, snapshot.comment_status) == (STATUS_SENDING, COMMENT_PENDING)
        attempt = _attempt(ledger, "attempt-1")
        assert attempt is not None
        assert (attempt.source_stage, attempt.comment_stage) == (STATUS_SENDING, COMMENT_PENDING)
    finally:
        close_ledger(ledger)


def test_重复准备同一尝试ID返回原结果不改写评论(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        first = _prepare(
            ledger, key, attempt_id="attempt-1", request_id="req-1", comment="原评论"
        )
        again = _prepare(
            ledger, key, attempt_id="attempt-1", request_id="req-2", comment="另一条评论"
        )

        assert first is not None and again is not None
        assert again == first, "同一尝试 ID 重复准备必须幂等返回原结果"
        attempt = _attempt(ledger, "attempt-1")
        assert attempt is not None and attempt.comment == "原评论"
    finally:
        close_ledger(ledger)


def test_阻断投递上的未消费准备在占位时作废(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _reserve(ledger, key, attempt_id="attempt-winner").granted is True
        assert _finalize(
            ledger,
            key,
            attempt_id="attempt-winner",
            status=STATUS_SENT,
            target_message_id="m-target-1",
        ) is True

        assert _prepare(ledger, key, attempt_id="attempt-late", comment="迟到的评论") is not None
        outcome = _reserve(ledger, key, attempt_id="attempt-late")

        assert outcome.granted is False
        assert (outcome.snapshot.status, outcome.snapshot.target_message_id) == (
            STATUS_SENT,
            "m-target-1",
        )
        attempt = _attempt(ledger, "attempt-late")
        assert attempt is not None
        assert attempt.source_stage == ATTEMPT_CANCELLED, "被阻断的未消费准备必须作废（明确未发送）"
    finally:
        close_ledger(ledger)


def test_尝试属于其它投递时被拒绝且不影响目标键(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        first = ledger_key(message_id="m-first")
        second = ledger_key(message_id="m-second")
        assert _prepare(ledger, first, attempt_id="attempt-first") is not None

        outcome = _reserve(ledger, second, attempt_id="attempt-first")

        assert outcome.granted is False, "尝试 ID 不能被跨投递重用来发送另一个投递"
        assert state_ledger_rows(tmp_path, message_id="m-second") == [], (
            "被拒绝的跨投递尝试不得在目标键留下占位"
        )
        assert _attempt(ledger, "attempt-first") is not None, "原尝试保留在原投递上"
        assert _attempt(ledger, "attempt-missing") is None, "不存在的尝试返回 None"

        prepared_twice = _prepare(ledger, second, attempt_id="attempt-first")
        assert prepared_twice is None, "尝试 ID 已被另一个投递占用：拒绝建立第二个身份"
    finally:
        close_ledger(ledger)
