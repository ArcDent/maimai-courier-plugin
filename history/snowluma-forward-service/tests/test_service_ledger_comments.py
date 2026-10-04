"""3.4 评论发送前占位与结果附属记录（design D7/D4「评论段固定顺序」）。

- 源未确认成功或未落账不能占位评论；
- 同一源尝试至多取得一次评论发送资格（重复提交只返回原结果）；
- 保存评论消息 ID 时不覆盖源目标 ID；
- 评论失败、未知或跳过都保留源已投递事实；
- 新工具调用（新尝试）不能重开评论阶段。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from service.ledger_store import ServiceLedger
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    SendStatus,
)
from shared.ledger import CommentReservation, DeliverySnapshot
from shared.protocol import CommentStatus, delivery_id_for

from tests.service_support import close_ledger, open_state_ledger, state_ledger_rows
from tests.support import ledger_key


def _prepare(
    ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str, comment: str = "附带评论"
) -> None:
    outcome = asyncio.run(
        ledger.prepare_attempt(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
            comment=comment,
        )
    )
    assert outcome is not None, "准备必须成功"


def _reserve(ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str) -> bool:
    return asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
        )
    ).granted


def _finalize_source(
    ledger: ServiceLedger,
    key: DeliveryKey,
    *,
    attempt_id: str,
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


def _reserve_comment(ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str) -> CommentReservation:
    return asyncio.run(ledger.reserve_comment(key, attempt_id=attempt_id))


def _finalize_comment(
    ledger: ServiceLedger,
    key: DeliveryKey,
    *,
    attempt_id: str,
    status: CommentStatus,
    comment_message_id: str = "",
    reason: str = "",
) -> bool:
    return asyncio.run(
        ledger.finalize_comment(
            key,
            attempt_id=attempt_id,
            status=status,
            comment_message_id=comment_message_id,
            reason=reason,
        )
    )


def _lookup(ledger: ServiceLedger, key: DeliveryKey) -> DeliverySnapshot:
    snapshot = asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))
    assert snapshot is not None
    return snapshot


def _sent_delivery(
    ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str, comment: str = "附带评论"
) -> None:
    """造出「源已确认成功且已落账」的投递（评论占位的前置条件）。"""

    _prepare(ledger, key, attempt_id=attempt_id, comment=comment)
    assert _reserve(ledger, key, attempt_id=attempt_id) is True
    assert _finalize_source(
        ledger,
        key,
        attempt_id=attempt_id,
        status=STATUS_SENT,
        target_message_id="m-target-1",
    ) is True


# ------------------------------------------------------------------- 占位门槛


def test_源未确认成功或未落账不能占位评论(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        _prepare(ledger, key, attempt_id="attempt-1")
        assert _reserve(ledger, key, attempt_id="attempt-1") is True

        in_flight = _reserve_comment(ledger, key, attempt_id="attempt-1")
        assert in_flight.granted is False, "源仍在途时不能占位评论"
        assert in_flight.snapshot.status == STATUS_SENDING

        assert _finalize_source(
            ledger, key, attempt_id="attempt-1", status=STATUS_FAILED, reason="确定未发出"
        ) is True
        failed = _reserve_comment(ledger, key, attempt_id="attempt-1")
        assert failed.granted is False, "源明确失败时不能占位评论"
        assert failed.snapshot.status == STATUS_FAILED

        unknown_key = ledger_key(message_id="m-unknown")
        _prepare(ledger, unknown_key, attempt_id="attempt-unknown")
        assert _reserve(ledger, unknown_key, attempt_id="attempt-unknown") is True
        assert _finalize_source(
            ledger, unknown_key, attempt_id="attempt-unknown", status=STATUS_UNKNOWN, reason="回执丢失"
        ) is True
        unknown = _reserve_comment(ledger, unknown_key, attempt_id="attempt-unknown")
        assert unknown.granted is False, "源待核实时不能占位评论"
        assert unknown.snapshot.status == STATUS_UNKNOWN

        granted_key = ledger_key(message_id="m-granted")
        _sent_delivery(ledger, granted_key, attempt_id="attempt-granted")
        granted = _reserve_comment(ledger, granted_key, attempt_id="attempt-granted")
        assert granted.granted is True, "源确认成功并落账后才授予评论资格"
        assert granted.snapshot.comment_status == "sending"
    finally:
        close_ledger(ledger)


def test_评论占位在独立连接可见(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        _sent_delivery(ledger, key, attempt_id="attempt-1")

        granted = _reserve_comment(ledger, key, attempt_id="attempt-1")

        assert granted.granted is True
        assert state_ledger_rows(tmp_path) == [("m-source-1", STATUS_SENT, "m-target-1")]
        row = asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))
        assert row is not None and row.comment_status == "sending", (
            "评论占位必须在返回前落盘"
        )
    finally:
        close_ledger(ledger)


def test_同一源尝试至多取得一次评论资格(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        _sent_delivery(ledger, key, attempt_id="attempt-1")

        assert _reserve_comment(ledger, key, attempt_id="attempt-1").granted is True
        second = _reserve_comment(ledger, key, attempt_id="attempt-1")
        assert second.granted is False, "同一尝试不得重复取得评论发送资格"
        assert second.snapshot.comment_status == "sending"

        assert _finalize_comment(
            ledger, key, attempt_id="attempt-1", status="sent", comment_message_id="m-comment-1"
        ) is True
        third = _reserve_comment(ledger, key, attempt_id="attempt-1")
        assert third.granted is False
        assert third.snapshot.comment_status == "sent"
    finally:
        close_ledger(ledger)


def test_评论未请求时不能占位(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key(message_id="m-no-comment")
        _prepare(ledger, key, attempt_id="attempt-1", comment="   ")
        assert _reserve(ledger, key, attempt_id="attempt-1") is True
        assert _finalize_source(
            ledger,
            key,
            attempt_id="attempt-1",
            status=STATUS_SENT,
            target_message_id="m-target-1",
        ) is True

        outcome = _reserve_comment(ledger, key, attempt_id="attempt-1")

        assert outcome.granted is False, "空白评论不请求评论段，不得占位"
        assert outcome.snapshot.comment_status == "not_requested"
    finally:
        close_ledger(ledger)


# ------------------------------------------------------------------- 结果附属


def test_保存评论消息ID不覆盖源目标ID(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        _sent_delivery(ledger, key, attempt_id="attempt-1")
        assert _reserve_comment(ledger, key, attempt_id="attempt-1").granted is True

        assert _finalize_comment(
            ledger, key, attempt_id="attempt-1", status="sent", comment_message_id="m-comment-7"
        ) is True

        snapshot = _lookup(ledger, key)
        assert (snapshot.status, snapshot.target_message_id) == (STATUS_SENT, "m-target-1")
        assert (snapshot.comment_status, snapshot.comment_message_id) == ("sent", "m-comment-7")
        assert state_ledger_rows(tmp_path) == [("m-source-1", STATUS_SENT, "m-target-1")], (
            "评论结果不得改写源目标 ID"
        )
    finally:
        close_ledger(ledger)


def test_评论失败未知与跳过都保留源已投递事实(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        for label, status, message_id in (
            ("失败", "failed", ""),
            ("未知", "unknown", ""),
            ("跳过", "skipped", ""),
        ):
            key = ledger_key(message_id=f"m-{status}")
            _sent_delivery(ledger, key, attempt_id=f"attempt-{status}")
            if status == "skipped":
                assert _finalize_comment(
                    ledger, key, attempt_id=f"attempt-{status}", status="skipped",
                    reason="客户端未提交评论挑战",
                ) is True, f"评论{label}必须可在占位前登记（未发送）"
            else:
                assert _reserve_comment(ledger, key, attempt_id=f"attempt-{status}").granted is True
                assert _finalize_comment(
                    ledger,
                    key,
                    attempt_id=f"attempt-{status}",
                    status=status,
                    reason=f"评论{label}",
                ) is True

            snapshot = _lookup(ledger, key)
            assert (snapshot.status, snapshot.target_message_id) == (STATUS_SENT, "m-target-1"), (
                f"评论{label}必须保留源已投递事实"
            )
            assert snapshot.comment_status == status
    finally:
        close_ledger(ledger)


def test_评论缺目标ID或契约外状态按待核实(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        no_id = ledger_key(message_id="m-comment-no-id")
        _sent_delivery(ledger, no_id, attempt_id="attempt-no-id")
        assert _reserve_comment(ledger, no_id, attempt_id="attempt-no-id").granted is True
        assert _finalize_comment(
            ledger, no_id, attempt_id="attempt-no-id", status="sent", comment_message_id=""
        ) is True
        snapshot = _lookup(ledger, no_id)
        assert snapshot.comment_status == "unknown", "缺真实评论消息 ID 不得登记为成功"
        assert "待核实" in snapshot.reason

        in_flight = ledger_key(message_id="m-comment-in-flight")
        _sent_delivery(ledger, in_flight, attempt_id="attempt-in-flight")
        assert _reserve_comment(ledger, in_flight, attempt_id="attempt-in-flight").granted is True
        assert _finalize_comment(
            ledger, in_flight, attempt_id="attempt-in-flight", status="sending"
        ) is True
        assert _lookup(ledger, in_flight).comment_status == "unknown", (
            "`sending` 不是评论终态，必须按待核实兜底"
        )
    finally:
        close_ledger(ledger)


def test_评论终态只命中占位且不误写其它键(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        _sent_delivery(ledger, key, attempt_id="attempt-1")

        assert _finalize_comment(
            ledger, key, attempt_id="attempt-1", status="sent", comment_message_id="m-x"
        ) is False, "没有评论占位时不得登记评论结果"

        other = ledger_key(message_id="m-other")
        _sent_delivery(ledger, other, attempt_id="attempt-other")
        assert _reserve_comment(ledger, other, attempt_id="attempt-other").granted is True

        assert _finalize_comment(
            ledger, key, attempt_id="attempt-other", status="failed"
        ) is False, "尝试必须属于该投递键才可登记评论结果"
        assert _lookup(ledger, key).comment_status == "pending"
        assert _lookup(ledger, other).comment_status == "sending"
    finally:
        close_ledger(ledger)


def test_新工具调用不能重开评论阶段(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        _sent_delivery(ledger, key, attempt_id="attempt-1")
        assert _reserve_comment(ledger, key, attempt_id="attempt-1").granted is True
        assert _finalize_comment(
            ledger, key, attempt_id="attempt-1", status="failed", reason="评论明确未发送"
        ) is True

        _prepare(ledger, key, attempt_id="attempt-2", comment="新工具调用的评论")
        assert _reserve(ledger, key, attempt_id="attempt-2") is False, (
            "成功投递不得开启新尝试"
        )
        reopened = _reserve_comment(ledger, key, attempt_id="attempt-2")
        assert reopened.granted is False, "新工具调用不能重开评论阶段"

        snapshot = _lookup(ledger, key)
        assert (snapshot.status, snapshot.comment_status) == (STATUS_SENT, "failed"), (
            "既有评论结果不得被新调用改写"
        )
    finally:
        close_ledger(ledger)
