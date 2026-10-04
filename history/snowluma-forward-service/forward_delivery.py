"""客户端投递编排：远端准备 → 最新复验 → 源阶段 → 固定延迟评论阶段（design D2/D4/D6/D8）。

顺序固定为：**本地预检通过 → 远端准备（只校验、查源与登记，不发送）→ 读最新配置
复验源许可 → 提交源阶段单次授权 → 源确认后固定等待 `COMMENT_DELAY_SECONDS` →
读最新配置复验评论许可 → 提交评论阶段单次授权 → 统一结果面**
（`render_forward_result`，口径见 `shared/result.py`）。

- 源与评论的「发送」只经 `RemoteServiceSession` 的两个阶段方法；本地编排不调用任何
  宿主发送能力（`host_send.py` 已退出调用图，没有 fallback），服务端账本是新投递的
  唯一权威（design D7）；
- 提交阶段授权前一律重新读**最新**配置（`source_allowed` / `comment_allowed` 闭包，
  组合根传入）：复验失败即不提交，并把已准备但不再允许的尝试尽力请求远端撤销；
  撤销不可达时如实记录「撤销未确认」，不伪称远端已停止；
- 执行响应丢失时只查询**同一投递**（`RemoteServiceSession.query`），不再提交发送
  探测；查询不可达一律按待核实报告；
- 源确认但服务端回执未落盘（公开原因文本）→ `sent=True`（已知真实目标 ID）但
  `success=False`，阶段为 `source_persist`；
- 评论结果未落盘 → `comment_status=unknown`，不声称评论结果可靠；
- 评论缺省、空或全空白 → 不等待、不提交评论阶段；源失败或未知不进评论段。

本模块从共享层 `shared/result.py` 重导出结果状态、类型与唯一结果渲染，插件侧既有
导入入口不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, Protocol

from .clock import Clock
from .forward_precheck import PreparedForward
from .remote_client import (
    CancelView,
    DeliveryView,
    ERROR_LEDGER_UNAVAILABLE,
    PrepareView,
    QueryMissing,
    RemoteRejected,
    RemoteServiceSession,
    RemoteUnavailable,
    StageView,
)
from .shared.delivery import STATUS_FAILED, STATUS_SENT, LedgerStatus, SendStatus
from .shared.result import (
    COMMENT_DELAY_SECONDS,
    STAGE_COMMENT_PERSIST,
    STAGE_COMMENT_SEND,
    STAGE_REJECTED,
    STAGE_SOURCE_PERSIST,
    STAGE_SOURCE_SEND,
    TOOL_STATUS_ALREADY_FORWARDED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_LEDGER_UNAVAILABLE,
    TOOL_STATUS_PARTIAL,
    TOOL_STATUS_REJECTED,
    TOOL_STATUS_SENDING,
    TOOL_STATUS_SENT,
    TOOL_STATUS_UNKNOWN,
    DeliveryResult,
    ErrorStage,
    SourceSendResult,
    ToolStatus,
    render_forward_result,
)
from .source_binding import NOTHING_SENT_NOTE, Rejected

PREPARE_STATUS_PREPARED: Final[str] = "prepared"
"""准备状态：已准备（可以提交源阶段授权）。"""

_SOURCE_UNPERSISTED_MARKERS: Final[tuple[str, ...]] = (
    "源发送结果未能落盘",
    "源发送结果未能命中在途占位",
)
"""服务端公开的成功回执未落盘原因文本（`service/coordinator.py::_complete_source`）。

命中即表示「平台已成功但持久事实未固定」：必须报告已知回执但不得宣称完整成功。
"""

_COMMENT_UNPERSISTED_MARKERS: Final[tuple[str, ...]] = (
    "评论结果未能落盘",
    "评论结果未能命中在途占位",
)
"""服务端公开的评论结果未落盘原因文本（`service/coordinator.py::_deliver_comment`）。"""

SendPermission = Callable[[], bool]
"""提交阶段授权前的权限复验：返回 False 表示不得提交（已关闭、开关关闭或白名单撤销）。

必须在**提交前**读最新配置，不得使用调用开始时的旧快照。
"""


@dataclass(frozen=True, slots=True)
class PendingAttempt:
    """已准备、尚未完成的尝试：供组合根在卸载/权限收缩时请求远端撤销。"""

    delivery_id: str
    attempt_id: str
    source_group_id: str
    target_group_id: str


class AttemptObserver(Protocol):
    """尝试生命周期观察者：组合根据此登记/释放需要在配置变化时撤销的尝试。"""

    def prepared(self, attempt: PendingAttempt) -> None: ...

    def finished(self, delivery_id: str) -> None: ...


class _NoAttempts:
    """缺省观察者：不登记任何尝试（无状态调用方）。"""

    def prepared(self, attempt: PendingAttempt) -> None:
        del attempt

    def finished(self, delivery_id: str) -> None:
        del delivery_id


NO_ATTEMPTS: Final[AttemptObserver] = _NoAttempts()


async def deliver_forward(
    *,
    session: RemoteServiceSession | RemoteUnavailable,
    prepared: PreparedForward,
    clock: Clock,
    request_id: str,
    source_allowed: SendPermission,
    comment_allowed: SendPermission,
    attempts: AttemptObserver = NO_ATTEMPTS,
) -> DeliveryResult:
    """执行一次转发：远端准备 → 源阶段 →（非空评论）固定等待后评论阶段。

    所有「没有发生发送」的失败都按 `rejected`（或服务端明确 `failed`）如实报告；
    已确认投递的事实与真实目标消息 ID 一律保留。
    """

    if isinstance(session, RemoteUnavailable):
        return _rejected(f"转发服务不可用（{session.reason}）")

    view = await session.prepare(
        request_id=request_id,
        source_group_id=prepared.source.group_id,
        source_message_id=prepared.arguments.message_id,
        target_group_id=prepared.arguments.target_group_id,
        comment=prepared.arguments.comment,
    )
    if isinstance(view, RemoteUnavailable):
        return _rejected(f"转发服务准备未完成（{view.reason}）")
    if isinstance(view, RemoteRejected):
        return _service_rejection(view, stage=STAGE_REJECTED)
    blocking = await _blocking_prepare_result(session, view)
    if blocking is not None:
        return blocking
    if view.status != PREPARE_STATUS_PREPARED:
        return DeliveryResult(
            status=TOOL_STATUS_UNKNOWN,
            stage=STAGE_SOURCE_PERSIST,
            reason=(
                f"转发服务返回了未识别的准备状态（{view.status}），结果待核实，"
                f"核实前不重发，{NOTHING_SENT_NOTE}"
            ),
        )
    if not view.challenge:
        return _rejected("转发服务未签发源阶段授权（准备不可发送）")

    attempt = PendingAttempt(
        delivery_id=view.delivery_id,
        attempt_id=view.attempt_id,
        source_group_id=prepared.source.group_id,
        target_group_id=prepared.arguments.target_group_id,
    )
    attempts.prepared(attempt)
    try:
        return await _run_stages(
            session=session,
            prepared=prepared,
            clock=clock,
            attempt=attempt,
            source_challenge=view.challenge,
            source_allowed=source_allowed,
            comment_allowed=comment_allowed,
        )
    finally:
        attempts.finished(view.delivery_id)


async def _blocking_prepare_result(
    session: RemoteServiceSession, view: PrepareView
) -> DeliveryResult | None:
    """既有成功/在途/待核实的准备结果：如实汇报，不发送也不补发评论。

    既有成功记录的评论状态为 `pending` 时（源已投递、评论尚未提交），只读查询一次
    同一投递：服务端借此把已失效的未提交评论登记为 `skipped`（第 5 组交接口径：
    客户端不得假设服务端会自行补发，会话被替换或失联后由客户端促成归并），客户端
    再按权威状态报告；查询不可达时按待核实报告，不伪称评论已发送或已跳过。
    """

    if view.status == "already_forwarded":
        comment_status = str(view.comment_status or "")
        reason = "同一源消息此前已投递到该目标群，本次未发送源消息或评论"
        if comment_status == "pending":
            settled = await session.query(view.delivery_id)
            if isinstance(settled, DeliveryView):
                comment_status = str(settled.comment_status or "")
                if comment_status == "pending":
                    comment_status = "unknown"
                    reason = (
                        "同一源消息此前已投递，但评论仍未完成（未提交授权已失效或尚未归并），"
                        "本次未发送源消息或评论"
                    )
            else:
                comment_status = "unknown"
                reason = (
                    "同一源消息此前已投递；评论未完成且只读查询不可达，评论结果待核实，"
                    "本次未发送源消息或评论"
                )
        if comment_status == "skipped":
            # 工具结果面只有 ""/sent/failed/unknown：已跳过 = 明确未发送。
            comment_status = STATUS_FAILED
            reason = (
                "同一源消息此前已投递；未提交的评论授权已失效，服务端已登记跳过，"
                "本次未发送源消息或评论"
            )
        elif comment_status not in ("", STATUS_SENT, STATUS_FAILED, "unknown"):
            comment_status = "unknown"
            reason = (
                f"同一源消息此前已投递，评论状态为 {comment_status}（待核实），"
                "本次未发送源消息或评论"
            )
        return DeliveryResult(
            status=TOOL_STATUS_ALREADY_FORWARDED,
            sent=True,
            already_forwarded=True,
            target_message_id=view.target_message_id,
            comment_status=comment_status,
            reason=reason,
        )
    if view.status == "sending":
        return DeliveryResult(
            status=TOOL_STATUS_SENDING,
            reason=f"同一源消息与目标群的投递正在发送中，{NOTHING_SENT_NOTE}",
        )
    if view.status == "unknown":
        return DeliveryResult(
            status=TOOL_STATUS_UNKNOWN,
            reason=(
                f"该投递已有待核实的记录（{view.target_message_id or '无目标消息 ID'}），"
                f"核实前不重发，{NOTHING_SENT_NOTE}"
            ),
        )
    return None


async def _run_stages(
    *,
    session: RemoteServiceSession,
    prepared: PreparedForward,
    clock: Clock,
    attempt: PendingAttempt,
    source_challenge: str,
    source_allowed: SendPermission,
    comment_allowed: SendPermission,
) -> DeliveryResult:
    """源阶段与评论阶段：每次提交授权前都按最新配置复验。"""

    if not source_allowed():
        note = await _cancel_attempt(session, attempt)
        return _rejected(
            f"源发送前权限复验未通过（插件已卸载、总开关关闭，或来源群/目标群已不在白名单）{note}"
        )

    outcome = await session.execute_source(
        delivery_id=attempt.delivery_id,
        attempt_id=attempt.attempt_id,
        challenge=source_challenge,
    )
    if isinstance(outcome, RemoteUnavailable):
        recovered = await _recover_source(session, attempt, outcome.reason)
        if recovered is None:
            return DeliveryResult(
                status=TOOL_STATUS_UNKNOWN,
                stage=STAGE_SOURCE_SEND,
                reason=(
                    "源阶段执行响应丢失且查询不可达，平台结果待核实，"
                    f"核实前不重发（不再次提交发送探测）：{outcome.reason}"
                ),
            )
        outcome = recovered
    if isinstance(outcome, RemoteRejected):
        return _service_rejection(outcome, stage=STAGE_REJECTED)

    source_result = _source_result(outcome)
    return await _deliver_comment(
        session=session,
        prepared=prepared,
        clock=clock,
        attempt=attempt,
        source_result=source_result,
        source_challenge=outcome.comment_challenge,
        comment_allowed=comment_allowed,
    )


async def _deliver_comment(
    *,
    session: RemoteServiceSession,
    prepared: PreparedForward,
    clock: Clock,
    attempt: PendingAttempt,
    source_result: DeliveryResult,
    source_challenge: str,
    comment_allowed: SendPermission,
) -> DeliveryResult:
    """评论段：源确认落账且评论非空白时，固定等待后复验权限再提交评论授权。"""

    if not prepared.arguments.comment.strip():
        return source_result
    if source_result.status != TOOL_STATUS_SENT:
        return source_result
    await clock.sleep(COMMENT_DELAY_SECONDS)
    if not comment_allowed():
        note = await _cancel_attempt(session, attempt)
        return _partial(
            source_result,
            comment_status=STATUS_FAILED,
            reason=(
                "评论发送前权限复验未通过（插件已卸载、总开关关闭，或来源群/目标群已不在白名单），"
                f"评论未发送{note}"
            ),
        )
    if not source_challenge:
        return _partial(
            source_result,
            comment_status="unknown",
            reason="源消息已投递，但评论阶段授权不可用（服务端未签发或已失效），评论未发送且不自动补发",
        )

    outcome = await session.execute_comment(
        delivery_id=attempt.delivery_id,
        attempt_id=attempt.attempt_id,
        challenge=source_challenge,
    )
    if isinstance(outcome, RemoteUnavailable):
        recovered = await _recover_comment(session, attempt, outcome.reason)
        if recovered is None:
            return _partial(
                source_result,
                comment_status="unknown",
                reason=(
                    "评论阶段执行响应丢失且查询不可达，评论结果待核实"
                    f"（保留源消息已投递事实，不自动补发）：{outcome.reason}"
                ),
            )
        outcome = recovered
    if isinstance(outcome, RemoteRejected):
        # 拒绝（常见于会话被替换/失联导致授权失效）：只读查询一次同一投递，
        # 让服务端把不再可提交的评论登记为 skipped，并按权威状态报告。
        settled = await session.query(attempt.delivery_id)
        if isinstance(settled, DeliveryView) and str(settled.comment_status or "") == "skipped":
            return _partial(
                source_result,
                comment_status=STATUS_FAILED,
                reason=(
                    f"评论阶段被服务端拒绝（{outcome.error}：{outcome.reason}）；"
                    "未提交的评论授权已失效，只读查询确认服务端已登记跳过，"
                    "源消息已投递，评论未发送且不自动补发"
                ),
            )
        return _partial(
            source_result,
            comment_status=STATUS_FAILED,
            reason=(
                f"评论阶段被服务端拒绝（{outcome.error}：{outcome.reason}），"
                "源消息已投递，评论未发送且不自动补发"
            ),
        )
    return _comment_result(source_result, outcome)


# --------------------------------------------------------------------------- 结果映射


def _source_result(view: StageView) -> DeliveryResult:
    """源阶段结果：成功必须有真实目标 ID；回执未落盘时报告已知事实但不宣称完整成功。"""

    target_message_id = str(view.target_message_id or "").strip()
    if view.status == STATUS_SENT and target_message_id:
        if _reason_hits(view.reason, _SOURCE_UNPERSISTED_MARKERS):
            return DeliveryResult(
                status=TOOL_STATUS_UNKNOWN,
                sent=True,
                target_message_id=target_message_id,
                stage=STAGE_SOURCE_PERSIST,
                reason=(
                    f"源消息已发送（已知真实目标消息 ID），但服务端未能把成功结果落盘"
                    f"（{view.reason}），结果待核实；本次未发送评论"
                ),
            )
        return DeliveryResult(
            status=TOOL_STATUS_SENT,
            sent=True,
            target_message_id=target_message_id,
            reason="源消息已发送并保存了真实目标消息 ID",
        )
    if view.status == STATUS_SENT:
        return DeliveryResult(
            status=TOOL_STATUS_UNKNOWN,
            stage=STAGE_SOURCE_SEND,
            reason="源阶段未给出真实目标消息 ID，结果待核实，核实前不重发",
        )
    if view.status == STATUS_FAILED:
        return DeliveryResult(
            status=TOOL_STATUS_FAILED,
            stage=STAGE_SOURCE_SEND,
            reason=f"{view.reason or '源消息明确未发送'}，{NOTHING_SENT_NOTE}",
        )
    return DeliveryResult(
        status=TOOL_STATUS_UNKNOWN,
        stage=STAGE_SOURCE_SEND,
        reason=(
            f"源阶段结果待核实（{view.reason or '回执不可确认'}），核实前不重发"
        ),
    )


def _comment_result(source_result: DeliveryResult, view: StageView) -> DeliveryResult:
    """评论阶段结果：评论结果未落盘时不声称可靠，源已投递事实一律保留。"""

    comment_status = str(view.comment_status or "")
    comment_message_id = str(view.comment_message_id or "").strip()
    if comment_status == STATUS_SENT and comment_message_id:
        if _reason_hits(view.reason, _COMMENT_UNPERSISTED_MARKERS):
            return _partial(
                source_result,
                comment_status="unknown",
                stage=STAGE_COMMENT_PERSIST,
                reason=(
                    f"源消息已投递，但评论结果未能落盘（{view.reason}），"
                    "不声称评论结果可靠，不自动补发评论"
                ),
            )
        return DeliveryResult(
            status=TOOL_STATUS_SENT,
            sent=True,
            target_message_id=source_result.target_message_id,
            comment_status=STATUS_SENT,
            comment_message_id=comment_message_id,
            reason="源消息与评论均已发送，并保存了真实目标消息 ID 与评论结果",
        )
    if comment_status == STATUS_SENT:
        return _partial(
            source_result,
            comment_status="unknown",
            stage=STAGE_COMMENT_PERSIST,
            reason="评论发送步骤未给出真实评论消息 ID，评论结果待核实，不自动补发",
        )
    if comment_status == STATUS_FAILED:
        return _partial(
            source_result,
            comment_status=STATUS_FAILED,
            reason=f"源消息已投递，但评论明确未发送（{view.reason or '服务端未发送评论'}）",
        )
    label = "评论已跳过" if comment_status == "skipped" else "评论结果待核实"
    return _partial(
        source_result,
        comment_status="unknown",
        reason=f"源消息已投递，但{label}（{view.reason or '回执不可确认'}），不自动补发评论",
    )


def _partial(
    source_result: DeliveryResult,
    *,
    comment_status: str,
    reason: str,
    stage: ErrorStage = STAGE_COMMENT_SEND,
) -> DeliveryResult:
    """部分成功：源消息已确认投递，评论未完成；保留源真实目标消息 ID。"""

    return DeliveryResult(
        status=TOOL_STATUS_PARTIAL,
        sent=True,
        target_message_id=source_result.target_message_id,
        comment_status=comment_status,
        stage=stage,
        reason=reason,
    )


def _service_rejection(rejection: RemoteRejected, *, stage: ErrorStage) -> DeliveryResult:
    """服务端明确拒绝：账本不可用单独表达，其余按「本次未发送任何消息」报告。"""

    if rejection.error == ERROR_LEDGER_UNAVAILABLE:
        return DeliveryResult(
            status=TOOL_STATUS_LEDGER_UNAVAILABLE,
            stage=STAGE_SOURCE_PERSIST,
            reason=f"服务端账本不可用（{rejection.reason}），已关闭发送，{NOTHING_SENT_NOTE}",
        )
    return DeliveryResult(
        status=TOOL_STATUS_REJECTED,
        stage=stage,
        reason=f"转发服务拒绝本次请求（{rejection.error}：{rejection.reason}），{NOTHING_SENT_NOTE}",
    )


def _rejected(reason: str) -> DeliveryResult:
    """按统一措辞构造拒绝结果：明确本次没有任何发送副作用。"""

    return DeliveryResult(
        status=TOOL_STATUS_REJECTED,
        stage=STAGE_REJECTED,
        reason=Rejected.not_sent(reason).reason,
    )


def _reason_hits(reason: str, markers: tuple[str, ...]) -> bool:
    """原因文本是否命中服务端公开的「未落盘」标记（回执已到、持久事实未固定）。"""

    return any(marker in reason for marker in markers)


# --------------------------------------------------------------------------- 响应丢失恢复


async def _recover_source(
    session: RemoteServiceSession,
    attempt: PendingAttempt,
    lost_reason: str,
) -> StageView | None:
    """源执行响应丢失：只查询同一投递；查询不可达返回 `None`（由调用方按待核实）。"""

    view = await session.query(attempt.delivery_id)
    if isinstance(view, DeliveryView):
        if view.status == STATUS_SENT and view.target_message_id:
            return StageView(
                delivery_id=attempt.delivery_id,
                attempt_id=attempt.attempt_id,
                stage="source",
                status=STATUS_SENT,
                target_message_id=view.target_message_id,
                comment_status=view.comment_status,
                comment_message_id=view.comment_message_id,
                comment_challenge=view.comment_challenge,
                comment_challenge_expires_in_seconds=view.comment_challenge_expires_in_seconds,
                reason=f"执行响应丢失（{lost_reason}），按同一投递的只读查询确认为已发送",
            )
        if view.status == STATUS_FAILED:
            return StageView(
                delivery_id=attempt.delivery_id,
                attempt_id=attempt.attempt_id,
                stage="source",
                status=STATUS_FAILED,
                reason=f"执行响应丢失（{lost_reason}），只读查询确认为明确未发送",
            )
        return StageView(
            delivery_id=attempt.delivery_id,
            attempt_id=attempt.attempt_id,
            stage="source",
            status="unknown",
            reason=f"执行响应丢失（{lost_reason}），只读查询状态为 {view.status}，结果待核实",
        )
    if isinstance(view, QueryMissing):
        # 账本没有该投递记录：源占位从未创建，平台调用不可能已开始（占位先于平台调用）。
        return StageView(
            delivery_id=attempt.delivery_id,
            attempt_id=attempt.attempt_id,
            stage="source",
            status=STATUS_FAILED,
            reason=f"执行响应丢失（{lost_reason}），只读查询确认没有该投递记录：平台调用未提交",
        )
    return None


async def _recover_comment(
    session: RemoteServiceSession,
    attempt: PendingAttempt,
    lost_reason: str,
) -> StageView | None:
    """评论执行响应丢失：只查询同一投递的评论列；查询不可达返回 `None`。"""

    view = await session.query(attempt.delivery_id)
    if isinstance(view, DeliveryView):
        return StageView(
            delivery_id=attempt.delivery_id,
            attempt_id=attempt.attempt_id,
            stage="comment",
            status=view.status,
            target_message_id=view.target_message_id,
            comment_status=view.comment_status,
            comment_message_id=view.comment_message_id,
            reason=f"执行响应丢失（{lost_reason}），按同一投递的只读查询恢复评论结果",
        )
    return None


async def _cancel_attempt(
    session: RemoteServiceSession,
    attempt: PendingAttempt,
) -> str:
    """尽力请求远端撤销未消费的尝试授权；不可达时返回「撤销未确认」说明。"""

    outcome = await session.cancel(
        delivery_id=attempt.delivery_id,
        attempt_id=attempt.attempt_id,
    )
    if isinstance(outcome, CancelView):
        return ""
    if isinstance(outcome, RemoteUnavailable):
        return f"；同时请求远端撤销但未确认（{outcome.reason}），远端可能仍保留未消费授权"
    return "；远端未确认撤销（服务端拒绝本次撤销请求）"


# 结果状态、类型与唯一结果渲染的唯一定义在共享层 `shared/result.py`；本模块重导出，
# 既有的 `forward_delivery.*` 导入入口与语义不变（design D9）。被重导出的名字包括
# `DeliveryResult` / `SourceSendResult` / `ToolStatus` / `render_forward_result` 与
# `STATUS_*` / `STAGE_*` / `TOOL_STATUS_*` 常量。
