"""本地投递编排：先查权威账本，再准备、占位、直连发送与可靠落账。"""
from __future__ import annotations

import asyncio
from typing import Callable

from .shared.clock import Clock
from .delivery_ledger import DeliveryLedger, DeliveryRecord, LedgerUnavailable, LedgerUnavailableError
from .forward_precheck import PreparedForward
from .shared.delivery import (DeliveryKey, LedgerStatus, SendStatus,
                              STATUS_FAILED, STATUS_SENT, STATUS_SENDING, STATUS_UNKNOWN)
from .shared.result import (
    COMMENT_DELAY_SECONDS, DeliveryResult, SourceSendResult, ToolStatus, ErrorStage,
    STAGE_REJECTED, STAGE_SOURCE_SEND, STAGE_SOURCE_PERSIST, STAGE_COMMENT_SEND, STAGE_COMMENT_PERSIST,
    TOOL_STATUS_SENT, TOOL_STATUS_PARTIAL, TOOL_STATUS_ALREADY_FORWARDED, TOOL_STATUS_SENDING,
    TOOL_STATUS_REJECTED, TOOL_STATUS_FAILED, TOOL_STATUS_UNKNOWN, TOOL_STATUS_LEDGER_UNAVAILABLE,
    render_forward_result,
)
from .shared.send_steps import PrepareRejected, PreparedSource, SourceSendRequest, CommentSendRequest
from .snowluma_client import SnowLumaClient
from .source_binding import Rejected

SendPermission = Callable[[], bool]


def _rejected(reason: str) -> DeliveryResult:
    return DeliveryResult(status=TOOL_STATUS_REJECTED, stage=STAGE_REJECTED,
                          reason=Rejected.not_sent(reason).reason)


def _existing(record: DeliveryRecord | None) -> DeliveryResult | None:
    """既有事实不引发任何平台查询、发送或补评论。"""
    if record is None or record.status == STATUS_FAILED:
        return None
    if record.status == STATUS_SENT:
        return DeliveryResult(status=TOOL_STATUS_ALREADY_FORWARDED, sent=True, already_forwarded=True,
                              target_message_id=record.target_message_id,
                              comment_status="unknown" if record.comment_status == STATUS_SENDING else record.comment_status,
                              comment_message_id=record.comment_message_id,
                              reason="同一源消息此前已投递，本次未发送源消息或评论")
    return DeliveryResult(status=TOOL_STATUS_SENDING if record.status == STATUS_SENDING else TOOL_STATUS_UNKNOWN,
                          reason="同一投递正在发送中，本次不重发" if record.status == STATUS_SENDING else "已有待核实记录，核实前不重发")


async def deliver_forward(*, prepared: PreparedForward, ledger: DeliveryLedger | LedgerUnavailable,
                          client: SnowLumaClient, clock: Clock, send_allowed: SendPermission,
                          stage_changed: Callable[[str], None] = lambda _: None) -> DeliveryResult:
    """公开编排入口；许可回调由组合根提供，最终交给传输提交边界。"""
    if isinstance(ledger, LedgerUnavailable):
        return DeliveryResult(status=TOOL_STATUS_LEDGER_UNAVAILABLE, stage=STAGE_SOURCE_PERSIST,
                              reason="本地账本不可用，本次未发送任何消息")
    key = DeliveryKey(prepared.source.account_id, prepared.source.group_id,
                      prepared.arguments.message_id, prepared.arguments.target_group_id)
    try:
        existing = _existing(ledger.get(key))
        if existing is not None:
            return existing
        stage_changed("源准备")
        source = await client.prepare_source(prepared)
        if isinstance(source, PrepareRejected):
            return _rejected(f"{source.error}：{source.reason}")
        if not send_allowed():
            return _rejected("源发送前权限复验未通过")
        reservation = ledger.reserve(key)
        if not reservation.granted:
            return _existing(reservation.record) or _rejected("未取得源发送占位")
    except LedgerUnavailableError:
        return DeliveryResult(status=TOOL_STATUS_LEDGER_UNAVAILABLE, stage=STAGE_SOURCE_PERSIST,
                              reason="本地账本不可用，本次未发送任何消息")
    stage_changed("源发送")
    try:
        outcome = await client.send_source(SourceSendRequest(source), before_submit=send_allowed)
    except (Exception, asyncio.CancelledError):
        outcome = SourceSendResult(STATUS_UNKNOWN, reason="源执行结果待核实")
    status = outcome.status if outcome.status in (STATUS_SENT, STATUS_FAILED) else STATUS_UNKNOWN
    if status == STATUS_SENT and not outcome.target_message_id:
        status = STATUS_UNKNOWN
    stage_changed("源落账")
    persist_reason = "源发送结果未能落盘"
    try:
        if status == STATUS_SENT:
            persisted = ledger.mark_sent(key, outcome.target_message_id)
        elif status == STATUS_FAILED:
            persisted = ledger.mark_failed(key, "明确未发送")
        else:
            persisted = ledger.mark_unknown(key, "发送结果待核实")
        if not persisted:
            persist_reason = "源发送结果未能命中在途占位"
            raise LedgerUnavailableError("源结果未命中在途占位")
    except LedgerUnavailableError:
        return DeliveryResult(status=TOOL_STATUS_UNKNOWN, sent=status == STATUS_SENT,
                              target_message_id=outcome.target_message_id if status == STATUS_SENT else "",
                              stage=STAGE_SOURCE_PERSIST,
                              reason=f"{persist_reason}，保留已知真实目标消息 ID，结果待核实，禁止重发；本次未发送评论")
    if status == STATUS_SENT:
        source_result = DeliveryResult(status=TOOL_STATUS_SENT, sent=True, target_message_id=outcome.target_message_id,
                                       reason="源消息已发送并保存真实目标消息 ID")
        if not prepared.arguments.comment.strip():
            return source_result
        return await _deliver_comment(ledger, key, client, source, prepared.arguments.comment,
                                      source_result, clock, send_allowed, stage_changed)
    if status == STATUS_FAILED and not send_allowed():
        return _rejected("源发送前权限复验未通过")
    return DeliveryResult(status=status, stage=STAGE_SOURCE_SEND,
                          reason="源消息明确未发送，本次未发送任何消息" if status == STATUS_FAILED else "源结果待核实，禁止重发")


def _partial(source: DeliveryResult, *, comment_status: str, reason: str,
             stage: ErrorStage = STAGE_COMMENT_SEND, message_id: str = "") -> DeliveryResult:
    return DeliveryResult(status=TOOL_STATUS_PARTIAL, sent=True, target_message_id=source.target_message_id,
                          comment_status=comment_status, comment_message_id=message_id, stage=stage, reason=reason)


async def _deliver_comment(ledger: DeliveryLedger, key: DeliveryKey, client: SnowLumaClient,
                           prepared: PreparedSource, comment: str, source: DeliveryResult,
                           clock: Clock, allowed: SendPermission,
                           stage_changed: Callable[[str], None]) -> DeliveryResult:
    stage_changed("评论等待")
    try:
        await clock.sleep(COMMENT_DELAY_SECONDS)
    except asyncio.CancelledError:
        return _partial(source, comment_status=STATUS_FAILED, reason="评论等待取消，评论未发送且不补发")
    if not allowed():
        return _partial(source, comment_status=STATUS_FAILED, reason="评论发送前权限复验未通过，评论未发送且不补发")
    stage_changed("评论占位")
    try:
        if not ledger.reserve_comment(key).granted:
            return _partial(source, comment_status=STATUS_UNKNOWN, reason="未取得评论占位，不提交评论")
    except LedgerUnavailableError:
        return _partial(source, comment_status=STATUS_UNKNOWN, stage=STAGE_COMMENT_PERSIST,
                        reason="评论占位失败，评论未发送，不声称评论结果可靠")
    stage_changed("评论发送")
    try:
        outcome = await client.send_comment(CommentSendRequest(prepared, comment), before_submit=allowed)
    except (Exception, asyncio.CancelledError):
        outcome = SourceSendResult(STATUS_UNKNOWN, reason="评论执行结果待核实")
    status = outcome.status if outcome.status in (STATUS_SENT, STATUS_FAILED) else STATUS_UNKNOWN
    if status == STATUS_SENT and not outcome.target_message_id:
        status = STATUS_UNKNOWN
    stage_changed("评论落账")
    try:
        if not ledger.record_comment(key, status=status,
                                     message_id=outcome.target_message_id if status == STATUS_SENT else ""):
            raise LedgerUnavailableError("评论结果未命中在途占位")
    except LedgerUnavailableError:
        return _partial(source, comment_status=STATUS_UNKNOWN, stage=STAGE_COMMENT_PERSIST,
                        message_id=outcome.target_message_id if status == STATUS_SENT else "",
                        reason="评论结果未能落盘，保留已知真实评论 ID；不声称评论结果可靠，不自动补发")
    if status == STATUS_SENT:
        return DeliveryResult(status=TOOL_STATUS_SENT, sent=True, target_message_id=source.target_message_id,
                              comment_status=STATUS_SENT, comment_message_id=outcome.target_message_id,
                              reason="源消息与评论均已发送并保存真实目标消息 ID")
    return _partial(source, comment_status=status,
                    reason="评论明确未发送，不自动补发" if status == STATUS_FAILED else "评论结果待核实，不自动补发")
