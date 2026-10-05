"""3.2 源发送占位与终态登记（design D7「服务端持久投递事实」）。

- 新键取得一次占位，且**独立连接**先能看到 `sending` 记录，编排才可能进入发送接缝
  （占位失败绝不发送：存储故障在 3.1 已断言为抛错且不留半条记录）；
- 成功、在途与未知都阻止再占位；只有明确未发的 `failed` 允许新的显式尝试；
- 确认成功必须保存真实目标消息 ID；缺 ID 或契约外终态一律按待核实兜底，
  未知不得被降级成可重试失败。
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
from shared.ledger import SourceReservation
from shared.protocol import delivery_id_for

from tests.service_support import close_ledger, open_state_ledger, state_ledger_rows
from tests.support import ledger_key


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
    status: SendStatus,
    attempt_id: str = "attempt-1",
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


def _lookup(ledger: ServiceLedger, key: DeliveryKey) -> object:
    return asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))


def test_新键占位在独立连接可见且初始评论状态未请求(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert state_ledger_rows(tmp_path) == [], "占位前账本里不得有任何记录"

        reservation = _reserve(ledger, key)

        assert reservation.granted is True
        assert reservation.snapshot.status == STATUS_SENDING
        assert reservation.snapshot.delivery_id == delivery_id_for(key)
        assert reservation.snapshot.comment_status == "not_requested"
        assert state_ledger_rows(tmp_path) == [("m-source-1", STATUS_SENDING, "")], (
            "占位必须在返回前落盘，独立连接可见后才可能进入发送接缝"
        )

        snapshot = _lookup(ledger, key)
        assert snapshot is not None
        assert snapshot.status == STATUS_SENDING, "占位返回的快照必须与账本查询一致"
    finally:
        close_ledger(ledger)


def test_成功在途与未知都阻止再占位(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        sent_key = ledger_key(message_id="m-sent")
        sending_key = ledger_key(message_id="m-sending")
        unknown_key = ledger_key(message_id="m-unknown")

        assert _reserve(ledger, sent_key, attempt_id="attempt-sent").granted is True
        assert _finalize(
            ledger,
            sent_key,
            attempt_id="attempt-sent",
            status=STATUS_SENT,
            target_message_id="m-target-1",
        ) is True

        assert _reserve(ledger, sending_key, attempt_id="attempt-sending").granted is True, (
            "在途记录由本次占位留下"
        )

        assert _reserve(ledger, unknown_key, attempt_id="attempt-unknown").granted is True
        assert _finalize(
            ledger, unknown_key, attempt_id="attempt-unknown", status=STATUS_UNKNOWN, reason="回执丢失"
        ) is True

        for key, expected in (
            (sent_key, STATUS_SENT),
            (sending_key, STATUS_SENDING),
            (unknown_key, STATUS_UNKNOWN),
        ):
            again = _reserve(ledger, key, attempt_id=f"attempt-again-{key.source_message_id}")
            assert again.granted is False, f"{expected} 必须阻止再占位（不得重复发送）"
            assert again.snapshot.status == expected

        assert _lookup(ledger, sent_key).target_message_id == "m-target-1", (
            "被拒绝的占位不得改写既有成功记录的真实目标 ID"
        )
    finally:
        close_ledger(ledger)


def test_只有明确未发的失败可以创建后续尝试(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _reserve(ledger, key).granted is True
        assert _finalize(
            ledger, key, status=STATUS_FAILED, reason="平台调用前确定未发出"
        ) is True

        retry = _reserve(ledger, key, attempt_id="attempt-retry")

        assert retry.granted is True, "明确未发送的失败才允许新的显式尝试"
        assert retry.snapshot.status == STATUS_SENDING
        snapshot = _lookup(ledger, key)
        assert (snapshot.status, snapshot.reason) == (STATUS_SENDING, ""), (
            "新尝试必须重置旧的失败原因与回执字段"
        )
        assert state_ledger_rows(tmp_path, message_id="m-source-1") == [
            ("m-source-1", STATUS_SENDING, "")
        ]
    finally:
        close_ledger(ledger)


def test_成功必须保存真实目标ID且缺ID按待核实(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        confirmed = ledger_key(message_id="m-confirmed")
        assert _reserve(ledger, confirmed, attempt_id="attempt-confirmed").granted is True
        assert _finalize(
            ledger,
            confirmed,
            attempt_id="attempt-confirmed",
            status=STATUS_SENT,
            target_message_id="m-target-9",
        ) is True
        snapshot = _lookup(ledger, confirmed)
        assert (snapshot.status, snapshot.target_message_id) == (STATUS_SENT, "m-target-9")

        no_id = ledger_key(message_id="m-no-id")
        assert _reserve(ledger, no_id, attempt_id="attempt-no-id").granted is True
        assert _finalize(
            ledger, no_id, attempt_id="attempt-no-id", status=STATUS_SENT, target_message_id=""
        ) is True
        snapshot = _lookup(ledger, no_id)
        assert snapshot.status == STATUS_UNKNOWN, "缺真实目标消息 ID 不得登记为成功"
        assert snapshot.target_message_id == ""
        assert "待核实" in snapshot.reason
        assert _reserve(ledger, no_id, attempt_id="attempt-after-unknown").granted is False
    finally:
        close_ledger(ledger)


def test_在途或契约外的终态登记都按待核实兜底(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        in_flight = ledger_key(message_id="m-still-sending")
        assert _reserve(ledger, in_flight).granted is True
        assert _finalize(ledger, in_flight, status=STATUS_SENDING) is True
        snapshot = _lookup(ledger, in_flight)
        assert snapshot.status == STATUS_UNKNOWN, (
            "`sending` 不是发送步骤的合法终态，必须按待核实兜底"
        )
        assert "待核实" in snapshot.reason
    finally:
        close_ledger(ledger)


def test_未知不能被降级为可重试失败(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _reserve(ledger, key).granted is True
        assert _finalize(ledger, key, status=STATUS_UNKNOWN, reason="回执不可确认") is True

        assert _finalize(ledger, key, status=STATUS_FAILED, reason="迟到的失败报告") is False, (
            "不在途的占位不接受终态登记"
        )
        snapshot = _lookup(ledger, key)
        assert snapshot.status == STATUS_UNKNOWN, "未知不得被降级为可重试失败"
        assert snapshot.reason == "回执不可确认"
        assert _reserve(ledger, key, attempt_id="attempt-after").granted is False
    finally:
        close_ledger(ledger)


def test_终态登记只命中在途占位且不误改其它键(tmp_path: Path) -> None:
    ledger = open_state_ledger(tmp_path)
    try:
        key = ledger_key()
        assert _finalize(ledger, key, status=STATUS_FAILED) is False, "未占位时不得凭空登记"

        assert _reserve(ledger, key).granted is True
        assert _finalize(
            ledger, key, status=STATUS_SENT, target_message_id="m-target-1"
        ) is True
        assert _finalize(ledger, key, status=STATUS_FAILED, reason="迟到") is False

        other = ledger_key(message_id="m-other")
        assert _reserve(ledger, other, attempt_id="attempt-other").granted is True
        assert _finalize(
            ledger, key, attempt_id="attempt-other", status=STATUS_FAILED
        ) is False, "尝试必须属于该投递键才可登记"

        snapshot = _lookup(ledger, key)
        assert (snapshot.status, snapshot.target_message_id, snapshot.reason) == (
            STATUS_SENT,
            "m-target-1",
            "",
        )
        assert _lookup(ledger, other).status == STATUS_SENDING, "另一个键的在途占位不受影响"
    finally:
        close_ledger(ledger)
