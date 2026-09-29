"""投递编排：持久占位、权限复验、源发送、回执落账、固定延迟评论与统一结果面（design D5/D6/D7）。

顺序固定为：**预检通过 → 账本占位（sending，短事务）→ 源发送前权限复验 →
源发送步骤 → 立即保存真实回执（sent）→ 非空评论等待 `COMMENT_DELAY_SECONDS` 秒 →
重验总开关与两级白名单 → 发送评论 → 保存评论结果并返回**（design D5/D7）。

- 占位失败（账本不可用）→ 不发送；
- 占位未获授权（同键已成功/在途/待核实）→ 如实汇报既有记录，不发送，也不补发评论；
- 源发送前的权限复验未通过（卸载、开关关闭或白名单撤销）→ 不调用发送接缝，把占位
  按「明确未发送」释放（可显式重试）并按拒绝报告；复验读**发送时**的最新配置；
- 发送步骤返回 `sent` 但缺少真实目标消息 ID → 按待核实处理（`sent=true` 不等于
  拿到回执，见第 1 组证据）；
- 发送步骤返回契约外的状态字面量（空串、大小写不符、超时/取消等未识别取值）→
  一律按待核实兜底，绝不降级成可安全重试的失败；
- 源发送明确失败但终态登记失败 → 账本里仍是未决占位，对外同样按待核实报告；
- 落账失败 → 停止后续评论并报告待核实，但保留已知回执供报告（不谎报成功）；
- 评论缺省/空/全空白 → 只转发、不等待；源发送未成功 → 不进入评论段；
- 评论未完成 → 返回部分成功：源消息已投递的事实与目标消息 ID 必须保留。

`SourceSendStep` 与 `CommentSendStep` 是本插件仅有的两个发送接缝，编排自身不调用
任何宿主发送能力（生产实现见 `host_send.py`），也不在网络等待期间持有数据库事务。
两个 `SendPermission`（源发送前、评论发送前）由组合根提供：卸载、总开关关闭或
来源群/目标群被撤销时都不得继续发送（design D7）。
统一结果面见 `render_forward_result`（`status` / `success` / `sent` / `comment_status`
/ `stage` 的关系在那里逐项写明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final, Literal, Union

from .clock import Clock
from .delivery_ledger import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    DeliveryLedger,
    DeliveryRecord,
    LedgerStatus,
    LedgerUnavailable,
    LedgerUnavailableError,
    SendStatus,
)
from .forward_precheck import PreparedForward
from .send_material import SendMaterial
from .source_binding import NOTHING_SENT_NOTE

ToolStatus = Union[
    LedgerStatus,
    Literal["partial", "already_forwarded", "rejected", "ledger_unavailable"],
]
"""工具结果状态面：账本四状态 + 「部分成功」+「已转发」+「拒绝」+「账本不可用」。"""

TOOL_STATUS_SENT: Final[ToolStatus] = STATUS_SENT
"""本次调用已确认源消息投递成功（评论未要求或已成功）。"""

TOOL_STATUS_PARTIAL: Final[ToolStatus] = "partial"
"""部分成功：源消息已确认投递并落账，但评论明确未发送或结果待核实。"""

TOOL_STATUS_ALREADY_FORWARDED: Final[ToolStatus] = "already_forwarded"
"""同键此前已成功投递：本次未产生任何新发送。"""

TOOL_STATUS_SENDING: Final[ToolStatus] = STATUS_SENDING
"""同键投递在途（并发调用只会发送一次）。"""

TOOL_STATUS_REJECTED: Final[ToolStatus] = "rejected"
"""预检或总开关拒绝：本次未发送任何消息。"""

TOOL_STATUS_UNKNOWN: Final[ToolStatus] = STATUS_UNKNOWN
"""结果待核实：可能已发送，必须阻止重发。"""

TOOL_STATUS_FAILED: Final[ToolStatus] = STATUS_FAILED
"""明确未发送的失败：允许后续显式重试。"""

TOOL_STATUS_LEDGER_UNAVAILABLE: Final[ToolStatus] = "ledger_unavailable"
"""投递账本不可用：已关闭发送，本次未发送任何消息。"""


@dataclass(frozen=True, slots=True)
class SourceSendResult:
    """源消息发送步骤的结果面。

    - `sent`：拿到真实目标消息 ID（`target_message_id` 必须非空）；
    - `failed`：明确没有发出任何消息；
    - `unknown`：超时、取消、回执丢失等无法确认的结果（调用方按待核实处理）。
    """

    status: SendStatus
    target_message_id: str = ""
    reason: str = ""


SourceSendStep = Callable[[PreparedForward], Awaitable[SourceSendResult]]
"""源发送步骤签名（真实实现见 `host_send.send_prepared_forward`）。"""

CommentSendStep = Callable[[str, str], Awaitable[SourceSendResult]]
"""评论发送步骤签名：`(目标会话 ID, 评论文本) -> SourceSendResult`（见 `host_send`）。"""

SendPermission = Callable[[], bool]
"""发送前的权限复验：返回 False 表示不得发送（已关闭、总开关关闭或白名单撤销）。

源发送前与评论发送前各调用一次：都必须读**发送时**的最新配置，不得使用调用开始时
的旧快照（design D7）。
"""

ErrorStage = Literal["", "rejected", "source_send", "source_persist", "comment_send", "comment_persist"]
"""错误阶段：拒绝 / 源发送 / 源持久化 / 评论发送 / 评论持久化（空串表示没有故障）。"""

STAGE_NONE: Final[ErrorStage] = ""
"""没有故障（完整成功、已转发、发送中等信息性结果）。"""

STAGE_REJECTED: Final[ErrorStage] = "rejected"
"""预检或总开关拒绝：本次未发送任何消息。"""

STAGE_SOURCE_SEND: Final[ErrorStage] = "source_send"
"""源消息发送阶段：明确未发送或结果待核实。"""

STAGE_SOURCE_PERSIST: Final[ErrorStage] = "source_persist"
"""源消息状态持久化阶段：占位或回执落盘失败。"""

STAGE_COMMENT_SEND: Final[ErrorStage] = "comment_send"
"""评论发送阶段：明确未发送或结果待核实。"""

STAGE_COMMENT_PERSIST: Final[ErrorStage] = "comment_persist"
"""评论结果持久化阶段：评论结果未能落盘。"""

COMMENT_DELAY_SECONDS: Final[float] = 0.5
"""评论固定延迟（秒）：代码常量，不是配置项，也不接受随机时长或模型参数覆盖。"""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """投递结果：供工具入口组装结果字典。

    `sent` 表示源消息**已确认投递**（本次或既有成功记录）；`delivered` 是其派生
    视图（同一语义，避免两个字段恒等重复）；`already_forwarded` 表示本次没有产生
    新的发送；`target_message_id` 是已取得的真实目标消息 ID；`comment_status` 是
    本记录上已保存的评论结果。
    """

    status: ToolStatus
    sent: bool = False
    already_forwarded: bool = False
    target_message_id: str = ""
    comment_status: str = ""
    comment_message_id: str = ""
    stage: ErrorStage = STAGE_NONE
    reason: str = ""

    @property
    def delivered(self) -> bool:
        """源消息是否已确认投递（本次或既有成功记录）；与 `sent` 同语义。"""

        return self.sent


async def deliver_forward(
    *,
    ledger: DeliveryLedger | LedgerUnavailable,
    prepared: PreparedForward,
    send_source: SourceSendStep,
    clock: Clock,
    send_comment: CommentSendStep,
    source_allowed: SendPermission,
    comment_allowed: SendPermission,
) -> DeliveryResult:
    """执行一次投递：占位 → 权限复验 → 发送步骤 → 回执落账 →（非空评论）延迟后评论。

    顺序固定为 design D5/D7：预检通过 → 持久占位 → **源发送前按最新配置复验权限** →
    源发送 → 立即保存真实回执 → 非空评论等待 0.5 秒 → **重验总开关与目标权限** →
    发送评论 → 保存评论结果并返回。本函数不调用任何宿主发送能力：源发送与评论发送
    都经注入的接缝（生产实现在 `host_send.py`）。

    权限复验失败时本次没有发出任何消息：占位按「明确未发送」释放（可重试），对外
    按拒绝报告，绝不降级成待核实，也不进入评论段。
    """

    if isinstance(ledger, LedgerUnavailable):
        return _ledger_unavailable_result(ledger.reason)

    key = delivery_key_for(prepared)
    try:
        reservation = ledger.reserve(key)
    except LedgerUnavailableError as error:
        return _ledger_unavailable_result(error.reason)
    if not reservation.granted:
        return _existing_result(reservation.record)

    if not source_allowed():
        return _source_revoked(ledger, key)

    outcome = await send_source(prepared)
    return await _deliver_comment(
        ledger=ledger,
        key=key,
        prepared=prepared,
        result=_finalize(ledger, key, outcome),
        clock=clock,
        send_comment=send_comment,
        comment_allowed=comment_allowed,
    )


def _source_revoked(ledger: DeliveryLedger, key: DeliveryKey) -> DeliveryResult:
    """源发送前权限复验未通过：不发送，并把占位释放为可重试的「明确未发送」。

    复验发生在实际调用发送接缝之前，因此本次确实没有任何发送副作用：占位应登记为
    `failed`（允许后续显式重试），而不是让它在重启后被误当成待核实。登记失败时仍按
    待核实保守报告（宁可人工核实，也不谎称可安全重试）。
    """

    reason = "源发送前权限复验未通过（插件已卸载、总开关关闭，或来源群/目标群已不在白名单）"
    if _record_failed(ledger, key, reason):
        return DeliveryResult(
            status=TOOL_STATUS_REJECTED,
            stage=STAGE_REJECTED,
            reason=f"{reason}，{NOTHING_SENT_NOTE}",
        )
    return DeliveryResult(
        status=TOOL_STATUS_UNKNOWN,
        stage=STAGE_SOURCE_PERSIST,
        reason=(
            f"{reason}，但未能把「明确未发送」登记到投递账本（存储失败），待核实；"
            f"{NOTHING_SENT_NOTE}"
        ),
    )


async def _deliver_comment(
    *,
    ledger: DeliveryLedger,
    key: DeliveryKey,
    prepared: PreparedForward,
    result: DeliveryResult,
    clock: Clock,
    send_comment: CommentSendStep,
    comment_allowed: SendPermission,
) -> DeliveryResult:
    """评论段：源发送成功落账且评论非空白时，固定等待后重验权限再独立发送评论。

    - 评论缺省、空字符串或全空白 → 只转发，不登记任何等待；
    - 源发送明确失败或结果未知 → 不进入评论段（`result.status` 不是 `sent`）；
    - 等待从源发送成功回执落账之后开始（本函数只在 `sent` 分支被调用），等待结束后
      以**发送时的最新配置**重验总开关与目标权限；
    - 评论失败不影响源消息已投递的事实：返回部分成功并保留真实目标消息 ID。
    """

    comment = prepared.arguments.comment
    if not comment.strip() or result.status != TOOL_STATUS_SENT:
        return result
    await clock.sleep(COMMENT_DELAY_SECONDS)
    if not comment_allowed():
        return _persist_comment_outcome(
            ledger,
            key,
            result,
            status=STATUS_FAILED,
            reason=(
                "评论发送前权限复验未通过（插件已卸载、总开关关闭，或来源群/目标群已不在白名单），"
                "评论未发送"
            ),
        )
    outcome = await send_comment(prepared.target.session_id, comment)
    message_id = str(outcome.target_message_id or "").strip()
    if str(outcome.status) == STATUS_SENT and message_id:
        return _persist_comment_outcome(
            ledger,
            key,
            result,
            status=STATUS_SENT,
            message_id=message_id,
        )
    if str(outcome.status) == STATUS_FAILED:
        return _persist_comment_outcome(
            ledger,
            key,
            result,
            status=STATUS_FAILED,
            reason=f"评论明确未发送（{outcome.reason or '宿主未发送'}）",
        )
    return _persist_comment_outcome(
        ledger,
        key,
        result,
        status=STATUS_UNKNOWN,
        reason=f"评论结果待核实（{outcome.reason or '回执不可确认'}）",
    )


def _persist_comment_outcome(
    ledger: DeliveryLedger,
    key: DeliveryKey,
    source: DeliveryResult,
    *,
    status: LedgerStatus,
    message_id: str = "",
    reason: str = "",
) -> DeliveryResult:
    """保存评论结果并组装结果：源消息已确认投递的事实不受评论结果影响。"""

    if not _record_comment(ledger, key, status=status, message_id=message_id):
        return DeliveryResult(
            status=TOOL_STATUS_PARTIAL,
            sent=True,
            target_message_id=source.target_message_id,
            comment_status=STATUS_UNKNOWN,
            stage=STAGE_COMMENT_PERSIST,
            reason=(
                "源消息已发送并保存了真实目标消息 ID，但评论结果未能保存到投递账本"
                f"（存储失败），待核实；{reason or '评论结果未登记'}，不会自动补发评论"
            ),
        )
    if status == STATUS_SENT:
        return DeliveryResult(
            status=TOOL_STATUS_SENT,
            sent=True,
            target_message_id=source.target_message_id,
            comment_status=STATUS_SENT,
            comment_message_id=message_id,
            reason="源消息与评论均已发送，并保存了真实目标消息 ID 与评论结果",
        )
    label = "评论明确未发送" if status == STATUS_FAILED else "评论结果待核实"
    return DeliveryResult(
        status=TOOL_STATUS_PARTIAL,
        sent=True,
        target_message_id=source.target_message_id,
        comment_status=status,
        comment_message_id=message_id,
        stage=STAGE_COMMENT_SEND,
        reason=f"源消息已发送并保存了真实目标消息 ID，但{label}：{reason}",
    )


def _record_comment(
    ledger: DeliveryLedger,
    key: DeliveryKey,
    *,
    status: LedgerStatus,
    message_id: str,
) -> bool:
    """保存评论结果；返回 False 表示未能写入（存储失败或没有可附着的成功记录）。"""

    try:
        return bool(ledger.record_comment(key, status=status, message_id=message_id))
    except LedgerUnavailableError:
        return False


def delivery_key_for(prepared: PreparedForward) -> DeliveryKey:
    """由预检结果构造账本键：账号 + 源群 + 源消息 ID + 目标群（评论不参与）。"""

    return DeliveryKey(
        account_id=prepared.source.account_id,
        source_group_id=prepared.source.group_id,
        source_message_id=prepared.arguments.message_id,
        target_group_id=prepared.target.group_id,
    )


def _ledger_unavailable_result(reason: str) -> DeliveryResult:
    """账本不可用：关闭发送并给出明确原因（本次确实没有任何发送副作用）。"""

    return DeliveryResult(
        status=TOOL_STATUS_LEDGER_UNAVAILABLE,
        stage=STAGE_SOURCE_PERSIST,
        reason=f"投递账本不可用（{reason}），已关闭发送，{NOTHING_SENT_NOTE}",
    )


def _existing_result(record: DeliveryRecord) -> DeliveryResult:
    """占位未获授权：按既有记录如实汇报，不产生任何发送。"""

    if record.status == STATUS_SENT:
        return DeliveryResult(
            status=TOOL_STATUS_ALREADY_FORWARDED,
            sent=True,
            already_forwarded=True,
            target_message_id=record.target_message_id,
            comment_status=record.comment_status,
            reason="同一源消息此前已投递到该目标群，本次未发送源消息或评论",
        )
    if record.status == STATUS_SENDING:
        return DeliveryResult(
            status=TOOL_STATUS_SENDING,
            reason=f"同一源消息与目标群的投递正在发送中，{NOTHING_SENT_NOTE}",
        )
    return DeliveryResult(
        status=TOOL_STATUS_UNKNOWN,
        reason=(
            f"存在待核实的投递记录（{record.reason or '结果未知'}），"
            f"核实前不重发，{NOTHING_SENT_NOTE}"
        ),
    )


def _finalize(
    ledger: DeliveryLedger,
    key: DeliveryKey,
    outcome: SourceSendResult,
) -> DeliveryResult:
    """按发送步骤的结果落账，并生成对外结果。"""

    target_message_id = str(outcome.target_message_id or "").strip()
    # 发送步骤来自可替换接缝：运行时可能给出契约外的字面量，这里按 str 收窄处理，
    # 未识别取值一律走最后的待核实兜底。
    send_status = str(outcome.status)

    if send_status == STATUS_SENT and target_message_id:
        return _persist_sent(ledger, key, target_message_id)

    if send_status == STATUS_SENT:
        # 缺少真实目标消息 ID：不能以 bool 充当成功证据。
        _record_unknown(ledger, key, "发送回执缺少真实目标消息 ID")
        return DeliveryResult(
            status=TOOL_STATUS_UNKNOWN,
            stage=STAGE_SOURCE_SEND,
            reason="发送步骤未给出真实目标消息 ID，结果待核实，核实前不重发",
        )

    if send_status == STATUS_UNKNOWN:
        _record_unknown(ledger, key, outcome.reason or "发送结果未知")
        return DeliveryResult(
            status=TOOL_STATUS_UNKNOWN,
            stage=STAGE_SOURCE_SEND,
            reason=f"发送结果待核实（{outcome.reason or '回执不可确认'}），核实前不重发",
        )

    if send_status == STATUS_FAILED:
        if _record_failed(ledger, key, outcome.reason or "明确未发送"):
            return DeliveryResult(
                status=TOOL_STATUS_FAILED,
                stage=STAGE_SOURCE_SEND,
                reason=f"{outcome.reason or '发送明确失败'}，{NOTHING_SENT_NOTE}",
            )
        # 终态登记失败：账本里仍是发送中占位（重启后会被归并为待核实），
        # 因此不得对外宣称「可重试的明确失败」。
        return DeliveryResult(
            status=TOOL_STATUS_UNKNOWN,
            stage=STAGE_SOURCE_PERSIST,
            reason=(
                f"{outcome.reason or '发送明确失败'}，但失败状态未能登记到投递账本（存储失败），"
                f"待核实；核实前不重发，{NOTHING_SENT_NOTE}"
            ),
        )

    # 契约外状态：可能已经发出但无法确认，按待核实处理（禁止重发）而不是 failed。
    _record_unknown(ledger, key, "发送步骤返回了未识别的状态")
    return DeliveryResult(
        status=TOOL_STATUS_UNKNOWN,
        stage=STAGE_SOURCE_SEND,
        reason="发送步骤返回了未识别的状态，结果待核实，核实前不重发",
    )


def _persist_sent(
    ledger: DeliveryLedger,
    key: DeliveryKey,
    target_message_id: str,
) -> DeliveryResult:
    """落账成功回执；落盘失败时保留已知回执并按待核实报告。"""

    try:
        persisted = ledger.mark_sent(key, target_message_id)
    except LedgerUnavailableError:
        return _sent_but_unregistered(
            ledger,
            key,
            target_message_id,
            cause="但投递状态未能落盘（存储失败）",
        )
    if persisted:
        return DeliveryResult(
            status=TOOL_STATUS_SENT,
            sent=True,
            target_message_id=target_message_id,
            reason="源消息已发送并保存了真实目标消息 ID",
        )
    return _sent_but_unregistered(
        ledger,
        key,
        target_message_id,
        cause="但投递状态未能登记（在途占位不可更新）",
    )


def _sent_but_unregistered(
    ledger: DeliveryLedger,
    key: DeliveryKey,
    target_message_id: str,
    *,
    cause: str,
) -> DeliveryResult:
    """发送已成功但状态未登记：停止后续评论，保留已知回执用于报告。"""

    _record_unknown(ledger, key, "源消息已发送但投递状态未能落盘")
    return DeliveryResult(
        status=TOOL_STATUS_UNKNOWN,
        sent=True,
        target_message_id=target_message_id,
        stage=STAGE_SOURCE_PERSIST,
        reason=f"源消息已发送（已知回执见 target_message_id），{cause}，待核实；本次未发送评论",
    )


def _record_unknown(ledger: DeliveryLedger, key: DeliveryKey, reason: str) -> None:
    """尽力补记待核实；补记本身失败不再改变对外结果（对外已按待核实兜底）。"""

    try:
        ledger.mark_unknown(key, reason)
    except LedgerUnavailableError:
        return


def _record_failed(ledger: DeliveryLedger, key: DeliveryKey, reason: str) -> bool:
    """尽力登记明确未发送；返回是否登记成功（失败时调用方不得宣称可重试）。"""

    try:
        return bool(ledger.mark_failed(key, reason))
    except LedgerUnavailableError:
        return False


def render_forward_result(
    result: DeliveryResult,
    *,
    material: SendMaterial | None = None,
    source_message_id: str = "",
    target_group_id: str = "",
) -> dict[str, Any]:
    """把投递结果组装成统一工具结果面（唯一的结果口径，见下）。

    口径关系：

    - `status`：调用状态，七种可区分取值——`sent`（完整成功）、`partial`（部分成功：
      源消息已投递并落账，评论未完成）、`already_forwarded`（此前已投递，本次未发送）、
      `sending`（同键在途）、`rejected`（预检/总开关拒绝）、`failed`（源消息明确未发送，
      可后续显式重试）、`unknown`（结果待核实，核实前不重发）；另有
      `ledger_unavailable`（账本不可用，属拒绝类）。
    - `success`：调用是否**完整成功**（`status` 为 `sent` 或 `already_forwarded`）；
      部分成功、待核实与已知回执但未落盘都不是完整成功。
    - `sent` / `delivered`：源消息是否已确认投递（拿到真实目标消息 ID），本次或既有
      记录都算；「已发送但状态未落盘」时为 true，但此时 `success` 仍为 false。
    - `comment_status`：""（未要求评论）/ `sent` / `failed` / `unknown`。
    - `stage`：错误阶段（`rejected` / `source_send` / `source_persist` / `comment_send`
      / `comment_persist`），无故障时为空串；`reason` 是配套的中文原因。
    - `material` 只在预检通过后传入：拒绝分支没有已准备的材料，不得声称材料完整。
    """

    rendered: dict[str, Any] = {
        "success": result.status in (TOOL_STATUS_SENT, TOOL_STATUS_ALREADY_FORWARDED),
        "status": result.status,
        "sent": result.sent,
        "delivered": result.delivered,
        "already_forwarded": result.already_forwarded,
        "target_message_id": result.target_message_id,
        "comment_status": result.comment_status,
        "comment_message_id": result.comment_message_id,
        "source_message_id": source_message_id,
        "target_group_id": target_group_id,
        "stage": result.stage,
        "reason": result.reason,
    }
    if material is not None:
        rendered["send_kind"] = material.kind
        rendered["material_shape"] = list(material.shape)
    return rendered
