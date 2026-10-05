"""服务端协调器：design D4 的九个公开接口在真实账本与真实网关上的实现。

本模块是 HTTP 层与「账本 + 网关 + 会话」之间的**唯一**业务编排点：

- 每个投递一把锁：撤销与「开始平台发送」在同一临界区定序（design D6），临界区内
  不持有 SQLite 事务等待网络；
- 阶段授权（挑战）由 `service.sessions` 持有并一次性消费；准备材料只在内存里短期
  存在，完成或过期后释放（不落账本）；评论原文会短暂写入账本尝试表（`prepare_attempt`）
  并在评论阶段从账本读取：落这一列只为崩溃后把未提交的评论判为跳过、不自动补发，
  它不写日志、不进响应，也不当作发送队列；
- 一切平台发送只经 `gateway.send_source` / `gateway.send_comment` 两个接缝，
  本模块不直接调用任何宿主或 OneBot 发送能力；
- 注入时钟覆盖会话到期、授权到期、2 秒开始窗口、0.5 秒评论最短间隔与两段退出等待。

本组按任务逐条接通：会话/心跳/权限（5.1–5.2）、准备（5.3）、源阶段（5.4）、
查询（5.5）、评论阶段（5.6）、撤销（5.7）、容量与隔离（5.8）、退出（5.9）。
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any, Final, Mapping

from shared.clock import Clock
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    LedgerStatus,
    LedgerUnavailableError,
    SendStatus,
)
from shared.ledger import (
    AttemptSnapshot,
    AttemptStage,
    CommentReservation,
    DeliverySnapshot,
    LedgerRepository,
    SourceReservation,
)
from shared.result import COMMENT_DELAY_SECONDS
from shared.protocol import (
    PREPARE_CHALLENGE_SECONDS,
    STAGE_COMMENT,
    STAGE_EXECUTION_WINDOW_SECONDS,
    STAGE_SOURCE,
    CancelAccepted,
    CancelRequest,
    CommentStatus,
    DeliveryState,
    HeartbeatRequest,
    PolicyAccepted,
    PolicyUpdateRequest,
    PrepareAccepted,
    PrepareRequest,
    SessionAlive,
    SessionClosed,
    SessionCreateRequest,
    SessionCreated,
    Stage,
    StageAuthorization,
    StageExecuted,
    StageExecutionRequest,
    delivery_id_for,
    delivery_key_for,
)
from shared.send_steps import CommentSendRequest, SourceMaterial, SourceSendRequest, normalize_send_status

from .backends import ServiceFault
from .gateway import SnowLumaGateway
from .http_api import (
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_FORBIDDEN,
    HTTP_NOT_FOUND,
    HTTP_SERVICE_UNAVAILABLE,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
)
from .ledger_store import (
    ATTEMPT_CANCELLED,
    ATTEMPT_PREPARED,
    ATTEMPT_SENDING,
    COMMENT_FAILED,
    COMMENT_PENDING,
    COMMENT_SKIPPED,
)
from .materials import PrepareRejected, PreparedSource
from .sessions import (
    ERROR_EXPIRED_AUTHORIZATION,
    ERROR_PERMISSION_DENIED,
    ERROR_SESSION_INVALID,
    ERROR_STALE_POLICY,
    SessionRejection,
    SessionRegistry,
    SessionView,
)

ERROR_ACCOUNT_CHANGED: Final[str] = "account_changed"
"""网关实际登录账号已变化：旧会话与未消费授权全部失效（不自动切换账号）。"""

ERROR_ACCOUNT_UNAVAILABLE: Final[str] = "account_unavailable"
"""网关账号未确认就绪：关闭发送，拒绝建立会话或执行阶段。"""

ERROR_LEDGER_UNAVAILABLE: Final[str] = "ledger_unavailable"
"""账本不可用：未落盘或未读到权威状态，一律关闭发送并如实报告。"""

ERROR_BUSY: Final[str] = "busy"
"""准备队列或执行并发已达容量上限：明确繁忙，不启动任何发送。"""

ERROR_ATTEMPT_CONFLICT: Final[str] = "attempt_conflict"
"""尝试 ID 已被另一个投递占用（内部冲突）：拒绝且不产生发送。"""

ERROR_NOT_FOUND: Final[str] = "not_found"
"""投递或尝试不存在（不是「空历史」，也不开启新尝试）。"""

ERROR_FORBIDDEN: Final[str] = "delivery_forbidden"
"""该投递属于另一个认证主体：拒绝读取或执行。"""

ERROR_EXECUTION_WINDOW_EXPIRED: Final[str] = "execution_window_expired"
"""阶段授权已在队列中超过 2 秒开始窗口：明确未发送，也不排队重发。"""

ERROR_MATERIAL_RELEASED: Final[str] = "material_released"
"""准备材料已过期或已释放：拒绝发送，不重建材料。"""

ERROR_SOURCE_NOT_SENT: Final[str] = "source_not_sent"
"""源消息尚未确认落账：评论阶段不得开始（不假装源已投递）。"""

ERROR_COMMENT_TOO_EARLY: Final[str] = "comment_too_early"
"""源回执后不足 0.5 秒：拒绝本次评论，且不排队定时发送。"""

ERROR_UNKNOWN_STAGE: Final[str] = "unknown_stage"
"""阶段执行收到未识别的阶段名：明确拒绝，不按源阶段兜底执行。"""

COMMENT_MIN_INTERVAL_SECONDS: Final[float] = COMMENT_DELAY_SECONDS
"""源回执到评论提交的服务端最短间隔（秒）：复用共享代码常量 0.5，不做成配置。"""

COMMENT_INTERVAL_TOLERANCE_SECONDS: Final[float] = 0.0001
"""最短间隔比较容差（秒）：只为浮点误差，不足以放过明显过早的提交。"""

MAX_PENDING_PREPARES: Final[int] = 64
"""准备中的源材料数量上限（代码常量）：超限明确「繁忙」，不驱逐也未启动任何发送。"""

MAX_CONCURRENT_EXECUTIONS: Final[int] = 8
"""同时在途的阶段执行数量上限（代码常量）：超限明确「繁忙」，不启动新的平台调用。"""

TASK_EXIT_WAIT_SECONDS: Final[float] = 5.0
"""服务退出时有界等待在途任务的上限（秒，代码常量；走注入时钟，不做真实长等待）。"""

TASK_CANCEL_WAIT_SECONDS: Final[float] = 1.0
"""超时后取消残留任务的收尾等待上限（秒，代码常量；走注入时钟，避免退出退化为无界）。"""

_ATTEMPT_SOURCE_STATUS: Final[Mapping[AttemptStage, SendStatus]] = {
    ATTEMPT_PREPARED: STATUS_SENDING,
    ATTEMPT_SENDING: STATUS_SENDING,
    STATUS_SENT: STATUS_SENT,
    STATUS_FAILED: STATUS_FAILED,
    STATUS_UNKNOWN: STATUS_UNKNOWN,
    ATTEMPT_CANCELLED: STATUS_FAILED,
}
"""尝试的源阶段 → 对外发送状态：未消费的准备属「待执行」、已撤销属「明确未发送」。"""


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """有界退出报告：等待秒数与超时未结束的在途阶段（可定位描述，逐条可告警）。"""

    waited_seconds: float
    leftover: tuple[str, ...] = ()


@dataclass(slots=True)
class _InFlight:
    """一个在途阶段执行的可定位描述（供并发上限、撤销定序与退出告警逐条列出）。"""

    delivery_id: str
    attempt_id: str
    stage: Stage

    def describe(self) -> str:
        """可定位描述：投递 / 尝试 / 阶段（不含正文与凭证）。"""

        return f"投递 {self.delivery_id} 尝试 {self.attempt_id} 阶段 {self.stage}"


@dataclass(slots=True)
class _PreparedSource:
    """一次准备的临时材料：只在内存里短期存在，完成或过期后释放（不落账本）。"""

    prepared: PreparedSource
    expires_at: float


def _fault(rejection: SessionRejection) -> ServiceFault:
    """控制状态拒绝 → 后端失败面（类别、原因与状态原样保留）。"""

    return ServiceFault(rejection.http_status, rejection.error, rejection.reason)


def _prepare_status(status: LedgerStatus) -> str:
    """投递阻断状态 → 准备结果状态（`sent` 对客户端表达为「此前已转发」）。"""

    return "already_forwarded" if status == STATUS_SENT else status


def _attempt_source_status(attempt: AttemptSnapshot) -> SendStatus:
    """尝试源阶段 → 发送状态：已撤销的准备属「明确未发送」（`failed`）。"""

    return _ATTEMPT_SOURCE_STATUS[attempt.source_stage]


class ServiceCoordinator:
    """服务端协调器：实现 `service.backends.ServiceBackends` 的九个接口。"""

    def __init__(
        self,
        *,
        ledger: LedgerRepository,
        gateway: SnowLumaGateway,
        sessions: SessionRegistry,
        clock: Clock,
        bound_subject: str,
        max_pending_prepares: int = MAX_PENDING_PREPARES,
        max_concurrent_executions: int = MAX_CONCURRENT_EXECUTIONS,
        task_exit_wait_seconds: float = TASK_EXIT_WAIT_SECONDS,
        task_cancel_wait_seconds: float = TASK_CANCEL_WAIT_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._gateway = gateway
        self._sessions = sessions
        self._clock = clock
        self._bound_subject = bound_subject
        self._max_pending_prepares = int(max_pending_prepares)
        self._max_concurrent_executions = int(max_concurrent_executions)
        self._task_exit_wait_seconds = float(task_exit_wait_seconds)
        self._task_cancel_wait_seconds = float(task_cancel_wait_seconds)
        self._prepared: dict[str, _PreparedSource] = {}
        # 已占下名额、尚未登记进 `_prepared` 的准备（容量硬保证：检查与登记之间不留窗口）。
        self._reserved_prepares = 0
        self._attempts_by_delivery: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict["asyncio.Task[Any]", _InFlight] = {}
        self._active: dict[str, _InFlight] = {}
        self._source_completed_at: dict[str, float] = {}

    @property
    def pending_prepares(self) -> int:
        """当前仍有效的准备材料数量（容量可观测面；只读、不发送）。"""

        self._purge_prepared()
        return len(self._prepared)

    @property
    def in_flight(self) -> tuple[str, ...]:
        """在途阶段的可定位描述（投递/尝试/阶段），供退出告警逐条列出。"""

        return tuple(info.describe() for info in self._tasks.values())

    @property
    def retained_source_marks(self) -> int:
        """仍保留「源回执时刻」的投递数量（内存有界性可观测面；评论终态即回收，只读）。"""

        return len(self._source_completed_at)

    async def shutdown(self) -> ShutdownReport:
        """有界等待在途任务退出；超时按投递/阶段列出残留并取消它们。

        只管理本协调器创建的任务：不触碰其它进程或插件的任务。两段等待都有上界
        （`TASK_EXIT_WAIT_SECONDS` 等正常退出、`TASK_CANCEL_WAIT_SECONDS` 等取消收尾），
        平台 RPC 即使吞掉取消也不会让退出退化为无界；被取消或未确定结束的已开始平台
        调用在账本里仍是 `sending` 占位，留待下次启动恢复为待核实（不自动重发、
        不恢复队列）。
        """

        pending = list(self._tasks)
        if not pending:
            return ShutdownReport(waited_seconds=0.0)
        started = self._clock.now()
        if await self._wait_for_tasks(pending, self._task_exit_wait_seconds):
            return ShutdownReport(waited_seconds=self._clock.now() - started)
        leftover = self.in_flight
        for task in pending:
            task.cancel()
        await self._wait_for_tasks(pending, self._task_cancel_wait_seconds)
        await asyncio.sleep(0)  # 让已完成任务的注销回调先执行，再判断真实残留
        still_pending = self.in_flight
        if still_pending:
            leftover = still_pending
        return ShutdownReport(
            waited_seconds=self._clock.now() - started,
            leftover=leftover,
        )

    async def _wait_for_tasks(self, tasks: list["asyncio.Task[Any]"], seconds: float) -> bool:
        """有界等待一组任务：返回是否在给定秒数（注入时钟）内全部结束。

        超时只返回 `False`，不在这里取消任何任务：调用方决定取消与告警，保证等待
        本身总有上界（平台调用即使在取消后仍吞掉取消，也不会阻塞退出）。
        """

        waiter = asyncio.gather(*tasks, return_exceptions=True)
        timer = asyncio.ensure_future(self._clock.sleep(seconds))
        try:
            done, _ = await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
            finished = waiter in done
        finally:
            if not timer.done():
                timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        if finished:
            await waiter
        return finished

    # ------------------------------------------------------------------ 会话

    async def create_session(
        self, request: SessionCreateRequest, *, subject: str
    ) -> SessionCreated | ServiceFault:
        """建立认证会话：只在账号就绪且客户端身份匹配时签发。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        if request.client_identity != self._bound_subject:
            return ServiceFault(
                HTTP_UNAUTHORIZED,
                ERROR_SESSION_INVALID,
                "请求的客户端身份与受控配置不一致，拒绝建立会话",
            )
        if self._gateway.ready is None:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_ACCOUNT_UNAVAILABLE,
                "网关账号尚未确认就绪：拒绝建立会话，也不进入可发送状态",
            )
        return self._sessions.open(subject, request.client_identity)

    async def heartbeat(
        self, session_id: str, request: HeartbeatRequest, *, subject: str
    ) -> SessionAlive | ServiceFault:
        """心跳：只续当前会话有效期，不更新权限、不发送、不查询网关。"""

        del request
        guard = self._account_guard()
        if guard is not None:
            return guard
        outcome = self._sessions.heartbeat(session_id, subject=subject)
        return _fault(outcome) if isinstance(outcome, SessionRejection) else outcome

    async def update_policy(
        self, session_id: str, request: PolicyUpdateRequest, *, subject: str
    ) -> PolicyAccepted | ServiceFault:
        """更新单调递增的权限代次与许可集合（服务端上限在视图里取交集）。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        outcome = self._sessions.update_policy(
            session_id,
            subject=subject,
            generation=request.policy_generation,
            enabled=request.enabled,
            source_groups=request.source_groups,
            target_groups=request.target_groups,
        )
        return _fault(outcome) if isinstance(outcome, SessionRejection) else outcome

    async def deregister(self, session_id: str, *, subject: str) -> SessionClosed | ServiceFault:
        """注销会话：未消费阶段授权失效；已保存的源回执不在这里删除。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        outcome = self._sessions.close(session_id, subject=subject)
        if isinstance(outcome, SessionRejection):
            return _fault(outcome)
        skipped = await self._skip_revoked_comments(
            outcome.revoked,
            "会话已注销：未消费的评论授权失效，评论登记为跳过（不自动补发）",
        )
        if isinstance(skipped, ServiceFault):
            return skipped
        return SessionClosed(session_id=outcome.session_id)

    # ------------------------------------------------------------------ 准备与阶段

    async def prepare(self, request: PrepareRequest, *, subject: str) -> PrepareAccepted | ServiceFault:
        """`POST /v1/forwards/prepare`：只校验、查源与登记，绝不发送。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        view, fault = self._stage_view(
            request.session_id, subject=subject, generation=request.policy_generation
        )
        if fault is not None:
            return fault
        assert view is not None
        if not view.allows_source(request.source_group_id):
            return ServiceFault(
                HTTP_FORBIDDEN,
                ERROR_PERMISSION_DENIED,
                "源群不在有效许可（客户端许可 ∩ 服务端上限）内：发送前拒绝",
            )
        if not view.allows_target(request.target_group_id):
            return ServiceFault(
                HTTP_FORBIDDEN,
                ERROR_PERMISSION_DENIED,
                "目标群不在有效许可（客户端许可 ∩ 服务端上限）内：发送前拒绝",
            )

        key = delivery_key_for(request, account_id=self._sessions.account_id)
        delivery_id = delivery_id_for(key)
        # 既有成功/在途/未知一律先返回其状态：不因源缓存后来失效而放行，也不查源。
        snapshot = await self._lookup_delivery(delivery_id)
        if isinstance(snapshot, ServiceFault):
            return snapshot
        if snapshot is not None and snapshot.status != STATUS_FAILED:
            return PrepareAccepted(
                delivery_id=delivery_id,
                attempt_id="",
                status=_prepare_status(snapshot.status),
                target_message_id=snapshot.target_message_id,
                comment_status=snapshot.comment_status,
            )

        self._purge_prepared()
        if len(self._prepared) + self._reserved_prepares >= self._max_pending_prepares:
            return ServiceFault(
                HTTP_TOO_MANY_REQUESTS,
                ERROR_BUSY,
                "准备队列已达容量上限：明确繁忙，未启动任何发送（成功/未知账本不被逐出）",
            )
        # 在等待查源之前占下名额：容量检查与占槽之间没有 await，多个并发准备不可能
        # 同时通过检查，上限因此是硬保证；成功登记或任一失败路径都在 finally 归还。
        self._reserved_prepares += 1
        try:
            prepared = await self._gateway.prepare_source(
                source_group_id=request.source_group_id,
                source_message_id=request.source_message_id,
                target_group_id=request.target_group_id,
            )
            if isinstance(prepared, PrepareRejected):
                return ServiceFault(HTTP_BAD_REQUEST, prepared.error, prepared.reason)

            attempt_id = f"a-{secrets.token_urlsafe(18)}"
            attempt = await self._register_attempt(key, attempt_id, request)
            if isinstance(attempt, ServiceFault):
                return attempt
            if attempt is None:
                return ServiceFault(
                    HTTP_CONFLICT,
                    ERROR_ATTEMPT_CONFLICT,
                    "尝试 ID 已被另一个投递占用，本次未登记任何可发送状态",
                )
            authorization = self._sessions.issue(
                stage=STAGE_SOURCE,
                delivery_id=delivery_id,
                attempt_id=attempt_id,
                session_id=request.session_id,
                policy_generation=view.policy_generation,
            )
            if isinstance(authorization, SessionRejection):
                # 未签发阶段授权：本次准备不可发送（材料不保留，重复调用可重新准备）。
                return _fault(authorization)
            self._remember_prepared(attempt_id, delivery_id, prepared)
            return PrepareAccepted(
                delivery_id=delivery_id,
                attempt_id=attempt_id,
                status=ATTEMPT_PREPARED,
                challenge=authorization.token,
                challenge_expires_in_seconds=PREPARE_CHALLENGE_SECONDS,
                comment_status=attempt.comment_stage,
            )
        finally:
            self._reserved_prepares -= 1

    async def execute_source(
        self, delivery_id: str, request: StageExecutionRequest, *, subject: str
    ) -> StageExecuted | ServiceFault:
        """`POST /v1/forwards/{delivery_id}/source`：源阶段单次授权与唯一平台提交。"""

        return await self._execute_stage(delivery_id, request, subject=subject, stage=STAGE_SOURCE)

    async def execute_comment(
        self, delivery_id: str, request: StageExecutionRequest, *, subject: str
    ) -> StageExecuted | ServiceFault:
        """`POST /v1/forwards/{delivery_id}/comment`（5.6 接通）。"""

        return await self._execute_stage(delivery_id, request, subject=subject, stage=STAGE_COMMENT)

    async def query(self, delivery_id: str, *, subject: str) -> DeliveryState | ServiceFault:
        """`GET /v1/forwards/{delivery_id}`：只读已持久化结果，绝不发送或补评论。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        attempt = await self._attempt_for_delivery(delivery_id)
        if isinstance(attempt, ServiceFault):
            return attempt
        if attempt is not None:
            ownership = self._ownership_fault(subject, attempt)
            if ownership is not None:
                return ownership
        elif subject != self._bound_subject:
            return ServiceFault(
                HTTP_FORBIDDEN,
                ERROR_FORBIDDEN,
                "该投递的原尝试已不在内存且请求主体不是受控绑定主体：拒绝读取",
            )
        snapshot = await self._lookup_delivery(delivery_id)
        if isinstance(snapshot, ServiceFault):
            return snapshot
        if snapshot is None:
            return self._not_found("没有该投递的账本记录（查询不启动任何发送，也不开启新尝试）")
        snapshot = await self._expire_orphan_comment(snapshot, attempt)
        if isinstance(snapshot, ServiceFault):
            return snapshot
        challenge, expires_in = ("", 0.0)
        if attempt is not None:
            challenge, expires_in = self._live_comment_challenge(
                delivery_id, attempt, subject=subject
            )
        return DeliveryState(
            delivery_id=delivery_id,
            status=snapshot.status,
            target_message_id=snapshot.target_message_id,
            comment_status=snapshot.comment_status,
            comment_message_id=snapshot.comment_message_id,
            comment_challenge=challenge,
            comment_challenge_expires_in_seconds=expires_in,
            reason=snapshot.reason,
        )

    async def cancel(
        self, delivery_id: str, request: CancelRequest, *, subject: str
    ) -> CancelAccepted | ServiceFault:
        """`POST /v1/forwards/{delivery_id}/cancel`：与「开始平台发送」在同一临界区定序。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        async with self._lock_for(delivery_id):
            active = self._active.get(delivery_id)
            if active is not None:
                return CancelAccepted(
                    delivery_id=delivery_id,
                    status="in_flight",
                    reason=f"{active.describe()} 已进入平台调用：不承诺撤回，按实际回执登记",
                )
            attempt_id = request.attempt_id or self._attempts_by_delivery.get(delivery_id, "")
            attempt = await self._lookup_attempt(attempt_id) if attempt_id else None
            if isinstance(attempt, ServiceFault):
                return attempt
            snapshot = await self._lookup_delivery(delivery_id)
            if isinstance(snapshot, ServiceFault):
                return snapshot
            if attempt is None or attempt.delivery_id != delivery_id:
                if snapshot is None:
                    return CancelAccepted(
                        delivery_id=delivery_id,
                        status="not_found",
                        reason="没有该投递的准备、尝试或未消费阶段授权",
                    )
                return CancelAccepted(
                    delivery_id=delivery_id,
                    status="completed",
                    reason=f"该投递已是 {snapshot.status}，没有未消费的阶段授权",
                )
            ownership = self._ownership_fault(subject, attempt)
            if ownership is not None:
                return ownership
            key = attempt.key or (snapshot.key if snapshot is not None else None)
            self._sessions.revoke_delivery(delivery_id)
            if attempt.source_stage == ATTEMPT_PREPARED:
                cancelled: bool | ServiceFault = True
                if key is not None:
                    cancelled = await self._cancel_attempt(
                        key,
                        attempt.attempt_id,
                        "客户端撤销：未消费的源与评论授权已失效，明确未发送",
                    )
                self._prepared.pop(attempt.attempt_id, None)
                if self._attempts_by_delivery.get(delivery_id) == attempt.attempt_id:
                    del self._attempts_by_delivery[delivery_id]
                if cancelled is not True:
                    # 授权已撤销，但撤销登记没有落盘：不伪称「已取消」，真实状态以账本为准
                    # （仍是可显式重试的未消费准备，绝不是待核实）。
                    return self._cancel_unconfirmed(cancelled)
                return CancelAccepted(
                    delivery_id=delivery_id,
                    status="cancelled",
                    reason="未消费的阶段授权已撤销：本次未发送任何消息（迟到请求不能重新激活）",
                )
            if attempt.source_stage == ATTEMPT_SENDING:
                return CancelAccepted(
                    delivery_id=delivery_id,
                    status="in_flight",
                    reason="源发送已开始：不承诺撤回，按实际回执登记（可能已产生副作用）",
                )
            skipped_note = ""
            if attempt.comment_stage == COMMENT_PENDING and key is not None:
                skipped = await self._skip_comment(
                    attempt,
                    key,
                    "客户端撤销：未消费的评论授权已失效，评论登记为跳过（不自动补发）",
                )
                if skipped is not True:
                    skipped_note = self._unconfirmed_note("评论跳过登记", skipped)
            return CancelAccepted(
                delivery_id=delivery_id,
                status="completed",
                reason=(
                    f"源已按 {attempt.source_stage} 结束；未消费的评论授权已撤销，"
                    f"已保存回执保留{skipped_note}"
                ),
            )

    # ------------------------------------------------------------------ 内部

    async def _execute_stage(
        self,
        delivery_id: str,
        request: StageExecutionRequest,
        *,
        subject: str,
        stage: Stage,
    ) -> StageExecuted | ServiceFault:
        """阶段执行入口：只读预检 → 独立任务执行（客户端断连不放弃已开始的平台调用）。"""

        submitted_at = self._clock.now()
        guard = self._account_guard()
        if guard is not None:
            return guard
        _, fault = self._stage_view(
            request.session_id, subject=subject, generation=request.policy_generation
        )
        if fault is not None:
            return fault
        attempt = await self._lookup_attempt(request.attempt_id)
        if isinstance(attempt, ServiceFault):
            return attempt
        if attempt is None or attempt.delivery_id != delivery_id:
            return self._not_found("没有该投递的对应尝试：拒绝执行，也不开启新尝试")
        ownership = self._ownership_fault(subject, attempt)
        if ownership is not None:
            return ownership
        if len(self._tasks) >= self._max_concurrent_executions:
            return ServiceFault(
                HTTP_TOO_MANY_REQUESTS,
                ERROR_BUSY,
                "同时在途的阶段执行已达并发上限：明确繁忙，未启动任何平台调用",
            )
        info = _InFlight(delivery_id=delivery_id, attempt_id=request.attempt_id, stage=stage)
        task = asyncio.ensure_future(
            self._deliver_stage(
                delivery_id=delivery_id,
                request=request,
                subject=subject,
                stage=stage,
                submitted_at=submitted_at,
                info=info,
            )
        )
        self._tasks[task] = info
        task.add_done_callback(self._task_finished)
        # 客户端断开只取消等待者：`shield` 让已开始的平台调用与结果登记继续完成。
        return await asyncio.shield(task)

    async def _deliver_stage(
        self,
        *,
        delivery_id: str,
        request: StageExecutionRequest,
        subject: str,
        stage: Stage,
        submitted_at: float,
        info: _InFlight,
    ) -> StageExecuted | ServiceFault:
        """在独立任务里执行一个阶段（源或评论）；未识别阶段明确拒绝，不按源兜底。"""

        if stage == STAGE_COMMENT:
            return await self._deliver_comment(
                delivery_id=delivery_id,
                request=request,
                subject=subject,
                submitted_at=submitted_at,
                info=info,
            )
        if stage != STAGE_SOURCE:
            return ServiceFault(
                HTTP_BAD_REQUEST,
                ERROR_UNKNOWN_STAGE,
                f"未识别的阶段 {stage!r}：拒绝执行，不按源阶段兜底发送",
            )
        return await self._deliver_source(
            delivery_id=delivery_id,
            request=request,
            subject=subject,
            submitted_at=submitted_at,
            info=info,
        )

    async def _deliver_source(
        self,
        *,
        delivery_id: str,
        request: StageExecutionRequest,
        subject: str,
        submitted_at: float,
        info: _InFlight,
    ) -> StageExecuted | ServiceFault:
        """源阶段：最终复验与占位在同一临界区，随后唯一一次平台 RPC（临界区外）。"""

        attempt = await self._lookup_attempt(request.attempt_id)
        if isinstance(attempt, ServiceFault):
            return attempt
        if attempt is None or attempt.delivery_id != delivery_id:
            return self._not_found("没有该投递的对应尝试：拒绝执行，也不开启新尝试")
        key = await self._key_for_attempt(attempt, delivery_id)
        if isinstance(key, ServiceFault):
            return key
        if attempt.source_stage != ATTEMPT_PREPARED:
            return await self._existing_stage_result(delivery_id, attempt, STAGE_SOURCE, None, subject)

        material: SourceMaterial | None = None
        async with self._lock_for(delivery_id):
            fault = self._final_stage_fault(request, subject=subject)
            if fault is not None:
                return fault
            current = await self._lookup_attempt(request.attempt_id)
            if isinstance(current, ServiceFault):
                return current
            if current is None or current.delivery_id != delivery_id:
                return self._not_found("尝试与投递身份不符：拒绝执行")
            if current.source_stage != ATTEMPT_PREPARED:
                return await self._existing_stage_result(
                    delivery_id, current, STAGE_SOURCE, None, subject
                )
            if self._clock.now() - submitted_at > STAGE_EXECUTION_WINDOW_SECONDS:
                return self._window_expired(STAGE_SOURCE)
            consumed = self._sessions.consume(
                request.challenge,
                expected_stage=STAGE_SOURCE,
                delivery_id=delivery_id,
                attempt_id=request.attempt_id,
                session_id=request.session_id,
                policy_generation=request.policy_generation,
            )
            if isinstance(consumed, SessionRejection):
                return _fault(consumed)
            material_entry = self._take_prepared(request.attempt_id)
            if material_entry is None:
                return ServiceFault(
                    HTTP_CONFLICT,
                    ERROR_MATERIAL_RELEASED,
                    "准备材料已过期或已释放：拒绝发送（可重新准备），不重建材料",
                )
            material = material_entry.prepared.material
            reservation = await self._reserve_source(key, current, request)
            if isinstance(reservation, ServiceFault):
                return reservation
            if not reservation.granted:
                return await self._existing_stage_result(
                    delivery_id, current, STAGE_SOURCE, reservation.snapshot, subject
                )
            # 占位已提交、进入唯一平台 RPC 前的**最后**复验（同一临界区，无未复验排队窗口）：
            # 权限/会话/总开关、2 秒开始窗口与材料有效性都在这里重新判定，失败即释放占位。
            if self._clock.now() - submitted_at > STAGE_EXECUTION_WINDOW_SECONDS:
                released = await self._release_placeholder(
                    key, current.attempt_id, "阶段授权在占位后超过 2 秒开始窗口：明确未发送"
                )
                if released is not True:
                    return self._release_unconfirmed(self._window_expired(STAGE_SOURCE), released)
                return self._window_expired(STAGE_SOURCE)
            last_fault = self._final_stage_fault(request, subject=subject)
            if last_fault is None and self._clock.now() > material_entry.expires_at:
                last_fault = ServiceFault(
                    HTTP_CONFLICT,
                    ERROR_MATERIAL_RELEASED,
                    "准备材料已过期：明确未发送，不重建材料",
                )
            if last_fault is not None:
                released = await self._release_placeholder(
                    key, current.attempt_id, f"{last_fault.reason}：本次明确未发送"
                )
                if released is not True:
                    return self._release_unconfirmed(last_fault, released)
                return last_fault
            self._active[delivery_id] = info

        result = await self._gateway.send_source(
            SourceSendRequest(
                delivery_id=delivery_id,
                attempt_id=attempt.attempt_id,
                target_group_id=key.target_group_id,
                material=material,
            )
        )
        return await self._complete_source(
            delivery_id=delivery_id,
            attempt=current,
            key=key,
            session_id=request.session_id,
            status=normalize_send_status(result.status),
            target_message_id=result.target_message_id,
            reason=result.reason,
        )

    async def _complete_source(
        self,
        *,
        delivery_id: str,
        attempt: AttemptSnapshot,
        key: DeliveryKey,
        session_id: str,
        status: SendStatus,
        target_message_id: str,
        reason: str,
    ) -> StageExecuted:
        """源 RPC 之后的落账与结果面：成功即附评论挑战；落盘失败不声称已可靠保存。"""

        finalized = await self._finalize_source(
            key, attempt.attempt_id, status, target_message_id, reason
        )
        persisted = finalized is True
        if status == STATUS_SENT and persisted:
            self._source_completed_at[delivery_id] = self._clock.now()
        challenge, expires_in = ("", 0.0)
        if status == STATUS_SENT and persisted:
            challenge, expires_in = self._issue_comment_challenge(
                delivery_id, attempt, session_id=session_id
            )
        comment_status = attempt.comment_stage
        final_reason = reason
        if isinstance(finalized, ServiceFault):
            final_reason = (
                f"{reason}；源发送结果未能落盘（{finalized.reason}），评论不再发送，"
                "该投递保持阻断"
            )
        elif finalized is False:
            final_reason = f"{reason}；源发送结果未能命中在途占位，按待核实处理"
        return StageExecuted(
            delivery_id=delivery_id,
            attempt_id=attempt.attempt_id,
            stage=STAGE_SOURCE,
            status=status,
            target_message_id=target_message_id,
            comment_status=comment_status,
            comment_challenge=challenge,
            comment_challenge_expires_in_seconds=expires_in,
            reason=final_reason,
        )

    async def _deliver_comment(
        self,
        *,
        delivery_id: str,
        request: StageExecutionRequest,
        subject: str,
        submitted_at: float,
        info: _InFlight,
    ) -> StageExecuted | ServiceFault:
        """评论阶段：源确认且满足 0.5 秒最短间隔后，才允许至多一次评论发送。"""

        attempt = await self._lookup_attempt(request.attempt_id)
        if isinstance(attempt, ServiceFault):
            return attempt
        if attempt is None or attempt.delivery_id != delivery_id:
            return self._not_found("没有该投递的对应尝试：拒绝执行，也不开启新尝试")
        key = await self._key_for_attempt(attempt, delivery_id)
        if isinstance(key, ServiceFault):
            return key
        if attempt.source_stage != STATUS_SENT:
            return ServiceFault(
                HTTP_CONFLICT,
                ERROR_SOURCE_NOT_SENT,
                "源消息尚未确认落账：不能发送评论（评论结果附属于源已投递事实）",
            )
        if attempt.comment_stage != COMMENT_PENDING:
            return await self._existing_stage_result(delivery_id, attempt, STAGE_COMMENT, None, subject)
        # 账本不变式：`comment_stage` 为 `pending` 时评论原文必非空（`prepare_attempt` 只对
        # 非空白评论登记 `pending`）。这里不设「空评论跳过」分支：空评论根本到不了评论阶段。
        assert attempt.comment.strip(), "账本不变式被破坏：pending 评论缺少原文"

        async with self._lock_for(delivery_id):
            fault = self._final_stage_fault(request, subject=subject)
            if fault is not None:
                return fault
            current = await self._lookup_attempt(request.attempt_id)
            if isinstance(current, ServiceFault):
                return current
            if current is None or current.delivery_id != delivery_id:
                return self._not_found("尝试与投递身份不符：拒绝执行")
            if current.source_stage != STATUS_SENT:
                return ServiceFault(
                    HTTP_CONFLICT, ERROR_SOURCE_NOT_SENT, "源消息尚未确认落账：不能发送评论"
                )
            if current.comment_stage != COMMENT_PENDING:
                return await self._existing_stage_result(
                    delivery_id, current, STAGE_COMMENT, None, subject
                )
            if self._clock.now() - submitted_at > STAGE_EXECUTION_WINDOW_SECONDS:
                return self._window_expired(STAGE_COMMENT)
            completed = self._source_completed_at.get(delivery_id)
            if (
                completed is None
                or self._clock.now() - completed
                < COMMENT_MIN_INTERVAL_SECONDS - COMMENT_INTERVAL_TOLERANCE_SECONDS
            ):
                # 不足最短间隔：拒绝本次提交，但**不消费挑战、不排队定时发送**，
                # 客户端可在满足间隔后用同一挑战重试。
                return ServiceFault(
                    HTTP_CONFLICT,
                    ERROR_COMMENT_TOO_EARLY,
                    "源回执后不足 0.5 秒：拒绝本次评论且不排队定时发送，"
                    "可在满足最短间隔后用同一挑战重试",
                )
            expired_skip = False
            consumed = self._sessions.consume(
                request.challenge,
                expected_stage=STAGE_COMMENT,
                delivery_id=delivery_id,
                attempt_id=request.attempt_id,
                session_id=request.session_id,
                policy_generation=request.policy_generation,
            )
            if isinstance(consumed, SessionRejection):
                if consumed.error == ERROR_EXPIRED_AUTHORIZATION:
                    expired_skip = True
                else:
                    return _fault(consumed)
            comment_reservation: CommentReservation | None = None
            if not expired_skip:
                reserved = await self._reserve_comment(key, current)
                if isinstance(reserved, ServiceFault):
                    return reserved
                if not reserved.granted:
                    return await self._existing_stage_result(
                        delivery_id, current, STAGE_COMMENT, reserved.snapshot, subject
                    )
                comment_reservation = reserved
                last_fault = self._final_stage_fault(request, subject=subject)
                if (
                    last_fault is None
                    and self._clock.now() - submitted_at > STAGE_EXECUTION_WINDOW_SECONDS
                ):
                    last_fault = self._window_expired(STAGE_COMMENT)
                if last_fault is not None:
                    released = await self._finalize_comment(
                        key,
                        current.attempt_id,
                        COMMENT_FAILED,
                        "",
                        f"{last_fault.reason}：本次明确未发送",
                    )
                    if released is not True:
                        return self._release_unconfirmed(last_fault, released, what="评论占位")
                    return last_fault
                self._active[delivery_id] = info
        if expired_skip:
            skipped = await self._skip_comment(
                current, key, "评论挑战已到期：登记跳过，服务器不自主补发"
            )
            note = "" if skipped is True else self._unconfirmed_note("评论跳过登记", skipped)
            return ServiceFault(
                HTTP_CONFLICT,
                ERROR_EXPIRED_AUTHORIZATION,
                f"评论挑战已到期：本次未发送，评论登记为跳过（不自主补发）{note}",
            )
        assert comment_reservation is not None

        result = await self._gateway.send_comment(
            CommentSendRequest(
                delivery_id=delivery_id,
                attempt_id=attempt.attempt_id,
                target_group_id=key.target_group_id,
                comment=attempt.comment,
            )
        )
        status = normalize_send_status(result.status)
        finalized = await self._finalize_comment(
            key, attempt.attempt_id, status, result.target_message_id, result.reason
        )
        reason = result.reason
        if isinstance(finalized, ServiceFault):
            reason = f"{reason}；评论结果未能落盘（{finalized.reason}），不声称评论结果已可靠保存"
        elif finalized is False:
            reason = f"{reason}；评论结果未能命中在途占位，按待核实处理"
        return StageExecuted(
            delivery_id=delivery_id,
            attempt_id=attempt.attempt_id,
            stage=STAGE_COMMENT,
            status=STATUS_SENT,
            target_message_id=attempt.target_message_id,
            comment_status=status,
            comment_message_id=result.target_message_id,
            reason=reason,
        )

    # ------------------------------------------------------------------ 结果面

    async def _existing_stage_result(
        self,
        delivery_id: str,
        attempt: AttemptSnapshot,
        stage: Stage,
        snapshot: DeliverySnapshot | None,
        subject: str,
    ) -> StageExecuted | ServiceFault:
        """重复执行/既有阻断：只返回原尝试与账本事实，不开启新尝试、不发送。"""

        if snapshot is None:
            current = await self._lookup_delivery(delivery_id)
            if isinstance(current, ServiceFault):
                return current
            snapshot = current
        return self._stage_result(delivery_id, attempt, snapshot, stage, subject=subject)

    def _stage_result(
        self,
        delivery_id: str,
        attempt: AttemptSnapshot,
        snapshot: DeliverySnapshot | None,
        stage: Stage,
        *,
        subject: str,
    ) -> StageExecuted:
        """把既有尝试 + 投递事实渲染成阶段结果（绝不发送、不从资源推断目标 ID）。"""

        status: SendStatus = snapshot.status if snapshot is not None else _attempt_source_status(attempt)
        target_message_id = (
            snapshot.target_message_id if snapshot is not None else attempt.target_message_id
        )
        comment_status = snapshot.comment_status if snapshot is not None else attempt.comment_stage
        comment_message_id = (
            snapshot.comment_message_id if snapshot is not None else attempt.comment_message_id
        )
        reason = snapshot.reason if snapshot is not None else attempt.reason
        challenge, expires_in = ("", 0.0)
        if stage == STAGE_SOURCE:
            challenge, expires_in = self._live_comment_challenge(
                delivery_id, attempt, subject=subject
            )
        return StageExecuted(
            delivery_id=delivery_id,
            attempt_id=attempt.attempt_id,
            stage=stage,
            status=status,
            target_message_id=target_message_id,
            comment_status=comment_status,
            comment_message_id=comment_message_id,
            comment_challenge=challenge,
            comment_challenge_expires_in_seconds=expires_in,
            reason=reason,
        )

    def _live_comment_challenge(
        self, delivery_id: str, attempt: AttemptSnapshot, *, subject: str
    ) -> tuple[str, float]:
        """原活动调用的未过期评论挑战：只回已签发的那一份，绝不重新生成。"""

        if attempt.comment_stage != COMMENT_PENDING or not attempt.comment.strip():
            return "", 0.0
        if self._sessions.active_session_for(subject) != attempt.session_id:
            return "", 0.0
        found = self._sessions.find(
            delivery_id=delivery_id, attempt_id=attempt.attempt_id, stage=STAGE_COMMENT
        )
        if found is None:
            return "", 0.0
        return found.token, max(0.0, found.expires_at - self._clock.now())

    def _issue_comment_challenge(
        self, delivery_id: str, attempt: AttemptSnapshot, *, session_id: str
    ) -> tuple[str, float]:
        """源成功且评论待执行时签发一次性评论挑战（同一尝试只签发一次）。"""

        if attempt.comment_stage != COMMENT_PENDING or not attempt.comment.strip():
            return "", 0.0
        authorization = self._sessions.issue(
            stage=STAGE_COMMENT,
            delivery_id=delivery_id,
            attempt_id=attempt.attempt_id,
            session_id=session_id,
            policy_generation=attempt.policy_generation,
        )
        if isinstance(authorization, SessionRejection):
            return "", 0.0
        return authorization.token, PREPARE_CHALLENGE_SECONDS

    # ------------------------------------------------------------------ 复验与守卫

    def _final_stage_fault(
        self, request: StageExecutionRequest, *, subject: str
    ) -> ServiceFault | None:
        """临界区内的最终复验：会话、权限代次、总开关与最新许可（读最新配置）。"""

        guard = self._account_guard()
        if guard is not None:
            return guard
        view, fault = self._stage_view(
            request.session_id, subject=subject, generation=request.policy_generation
        )
        if fault is not None:
            return fault
        assert view is not None
        return None

    def _ownership_fault(self, subject: str, attempt: AttemptSnapshot) -> ServiceFault | None:
        """投递归属：只有原主体（或其受控绑定主体）可以读写该投递。"""

        owner = self._sessions.session_subject(attempt.session_id)
        if owner:
            if owner == subject:
                return None
            return ServiceFault(HTTP_FORBIDDEN, ERROR_FORBIDDEN, "该投递属于另一个认证主体：拒绝访问")
        if subject == self._bound_subject:
            return None
        return ServiceFault(
            HTTP_FORBIDDEN,
            ERROR_FORBIDDEN,
            "该投递的原会话已失效且不属于受控绑定主体：拒绝访问",
        )

    def _not_found(self, reason: str) -> ServiceFault:
        return ServiceFault(HTTP_NOT_FOUND, ERROR_NOT_FOUND, reason)

    def _window_expired(self, stage: Stage) -> ServiceFault:
        return ServiceFault(
            HTTP_CONFLICT,
            ERROR_EXECUTION_WINDOW_EXPIRED,
            f"{stage} 阶段提交后超过 2 秒开始窗口：明确未发送（也不排队重发），请重新准备",
        )

    def _release_unconfirmed(
        self, original: ServiceFault, outcome: bool | ServiceFault, *, what: str = "源占位"
    ) -> ServiceFault:
        """释放占位（源或评论）未能确认落盘：如实说明真实状态，不伪称已释放。

        调用点已确认本进程没有调用平台发送，但账本事实尚未更新（占位仍是 `sending`，
        重启后归并为待核实）；因此对外不报告「明确未发送的干净失败」，也不降级为
        可重试失败，而是保留原拒绝类别并在原因里说明未落盘与待核实口径。
        """

        return ServiceFault(
            original.http_status,
            original.error,
            f"{original.reason}{self._unconfirmed_note(f'{what}释放', outcome)}",
        )

    def _cancel_unconfirmed(self, outcome: bool | ServiceFault) -> ServiceFault:
        """撤销登记未能确认落盘：授权已失效，但不伪称已撤销、也不降级为待核实。

        账本里该尝试仍是可显式重试的未消费准备（绝不是「可能已发送」的待核实状态）；
        迟到请求因授权已撤销不能发送，客户端可以重新查询后显式重试撤销。
        """

        if isinstance(outcome, ServiceFault):
            return ServiceFault(
                outcome.http_status,
                outcome.error,
                f"{outcome.reason}；未消费的阶段授权已失效（迟到请求不能发送），"
                "但账本状态可能仍是未消费的准备：本次不报告已撤销，可显式重试撤销",
            )
        return ServiceFault(
            HTTP_CONFLICT,
            ERROR_ATTEMPT_CONFLICT,
            "撤销未命中该未消费的准备（账本状态可能已变化）：本次不报告已撤销，可重新查询后重试",
        )

    def _unconfirmed_note(self, what: str, outcome: bool | ServiceFault) -> str:
        """未确认落盘的统一补充说明（不伪称已登记，真实状态以账本为准）。"""

        if isinstance(outcome, ServiceFault):
            detail = outcome.reason
        else:
            detail = "未命中对应记录"
        return (
            f"；{what}未能确认落盘（{detail}）：按未确认处理，真实状态以账本为准"
            "（不伪称已登记；若账本仍是发送占位，重启后归并为待核实）"
        )

    def _lock_for(self, delivery_id: str) -> asyncio.Lock:
        """每个投递一把锁：撤销与「开始平台发送」在同一临界区定序（design D6）。

        锁对象按投递 ID 常驻本进程生命周期，不随投递终态回收：一旦回收，同一投递就
        可能同时存在两把不同的锁（等待者与新进入者各持一把），临界区互斥随即失效。
        单进程长驻服务里锁对象极小，数量由本进程处理过的投递数决定，属可接受的常驻成本。
        """

        lock = self._locks.get(delivery_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[delivery_id] = lock
        return lock

    def _task_finished(self, task: "asyncio.Task[Any]") -> None:
        """在途任务结束：注销登记并取回异常（结果已由账本/响应表达）。"""

        info = self._tasks.pop(task, None)
        if info is not None and self._active.get(info.delivery_id) is info:
            del self._active[info.delivery_id]
        if not task.cancelled():
            task.exception()

    async def _key_for_attempt(
        self, attempt: AttemptSnapshot, delivery_id: str
    ) -> DeliveryKey | ServiceFault:
        """尝试对应的投递四字段身份：优先取尝试行自带的键，缺失时回查投递表。"""

        if attempt.key is not None:
            return attempt.key
        snapshot = await self._lookup_delivery(delivery_id)
        if isinstance(snapshot, ServiceFault):
            return snapshot
        if snapshot is None:
            return self._not_found("没有该投递的账本记录：拒绝执行")
        return snapshot.key

    async def _reserve_source(
        self, key: DeliveryKey, attempt: AttemptSnapshot, request: StageExecutionRequest
    ) -> SourceReservation | ServiceFault:
        """源发送前占位（短事务，返回前提交）：占位失败一律不发送。"""

        try:
            return await self._ledger.reserve_source(
                key,
                attempt_id=attempt.attempt_id,
                request_id=attempt.request_id,
                session_id=request.session_id,
                policy_generation=request.policy_generation,
            )
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"源发送占位未能落盘（{error.reason}）：本次未发送任何消息，拒绝继续",
            )

    async def _reserve_comment(
        self, key: DeliveryKey, attempt: AttemptSnapshot
    ) -> CommentReservation | ServiceFault:
        """评论发送前占位：源未确认成功或未落账时不授予（同一尝试至多一次）。"""

        try:
            return await self._ledger.reserve_comment(key, attempt_id=attempt.attempt_id)
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"评论发送占位未能落盘（{error.reason}）：本次未发送任何消息，拒绝继续",
            )

    async def _finalize_source(
        self,
        key: DeliveryKey,
        attempt_id: str,
        status: SendStatus,
        target_message_id: str,
        reason: str,
    ) -> bool | ServiceFault:
        """登记源阶段终态；存储失败返回 `ServiceFault`（不把已知回执说成已落盘）。"""

        try:
            return await self._ledger.finalize_source(
                key,
                attempt_id=attempt_id,
                status=status,
                target_message_id=target_message_id,
                reason=reason,
            )
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"源发送结果未能落盘（{error.reason}）",
            )

    async def _release_placeholder(
        self, key: DeliveryKey, attempt_id: str, reason: str
    ) -> bool | ServiceFault:
        """把已提交但不再发送的源占位按「明确未发送」释放（可显式重试，不降级为待核实）。

        返回 `True` 表示释放已落盘（账本为 `failed`）；`False` 表示未命中在途占位；
        `ServiceFault` 表示落盘失败——此时账本仍停在 `sending`，调用方必须如实说明
        （重启后归并为待核实），不得伪称已释放。
        """

        return await self._finalize_source(key, attempt_id, STATUS_FAILED, "", reason)

    # ------------------------------------------------------------------ 查询辅助

    async def _attempt_for_delivery(self, delivery_id: str) -> AttemptSnapshot | ServiceFault | None:
        """取该投递最近一次尝试的定位（内存加速器；重启后为空，不影响只读结果）。"""

        attempt_id = self._attempts_by_delivery.get(delivery_id, "")
        if not attempt_id:
            return None
        attempt = await self._lookup_attempt(attempt_id)
        if isinstance(attempt, ServiceFault):
            return attempt
        return attempt

    async def _expire_orphan_comment(
        self, snapshot: DeliverySnapshot, attempt: AttemptSnapshot | None
    ) -> DeliverySnapshot | ServiceFault:
        """评论挑战已到期或被撤销时把未提交评论登记为跳过（不形成永久待发送队列）。

        只读查询接口自身从不发送：这里登记的 `skipped` 是「不会发送」的终态，
        与 `sending`/`unknown` 一样阻止后续自动补发。
        """

        if (
            attempt is None
            or snapshot.status != STATUS_SENT
            or snapshot.comment_status != COMMENT_PENDING
        ):
            return snapshot
        live = self._sessions.find(
            delivery_id=snapshot.delivery_id, attempt_id=attempt.attempt_id, stage=STAGE_COMMENT
        )
        if live is not None:
            return snapshot
        finalized = await self._skip_comment(
            attempt, snapshot.key, "评论挑战已到期或已撤销：未提交评论登记为跳过，服务器不自主补发"
        )
        if isinstance(finalized, ServiceFault):
            return finalized
        refreshed = await self._lookup_delivery(snapshot.delivery_id)
        if isinstance(refreshed, ServiceFault):
            return refreshed
        return refreshed if refreshed is not None else snapshot

    async def _skip_comment(
        self, attempt: AttemptSnapshot, key: DeliveryKey, reason: str
    ) -> bool | ServiceFault:
        """把未提交的评论登记为跳过（`pending` → `skipped`），绝不产生平台调用。"""

        return await self._finalize_comment(key, attempt.attempt_id, COMMENT_SKIPPED, "", reason)

    async def _finalize_comment(
        self,
        key: DeliveryKey,
        attempt_id: str,
        status: CommentStatus,
        comment_message_id: str,
        reason: str,
    ) -> bool | ServiceFault:
        """保存评论结果；存储失败返回 `ServiceFault`（不声称评论结果已知）。

        评论一旦进入终态（含 `skipped`），该投递的源回执时刻就只在本次调用之前有用：
        随之回收，避免长驻进程为已完成投递无限保留计时项（落盘失败时保留，状态仍以
        账本为准）。
        """

        try:
            finalized = await self._ledger.finalize_comment(
                key,
                attempt_id=attempt_id,
                status=status,
                comment_message_id=comment_message_id,
                reason=reason,
            )
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"评论结果未能落盘（{error.reason}）",
            )
        self._source_completed_at.pop(delivery_id_for(key), None)
        return finalized

    async def _skip_revoked_comments(
        self, revoked: tuple[StageAuthorization, ...], reason: str
    ) -> ServiceFault | None:
        """注销/替换会话时，把失去评论授权的待执行评论登记为跳过（不自动补发）。"""

        for authorization in revoked:
            if authorization.stage != STAGE_COMMENT:
                continue
            attempt = await self._lookup_attempt(authorization.attempt_id)
            if isinstance(attempt, ServiceFault):
                return attempt
            if attempt is None or attempt.comment_stage != COMMENT_PENDING:
                continue
            key = await self._key_for_attempt(attempt, authorization.delivery_id)
            if isinstance(key, ServiceFault):
                continue  # 没有投递记录时评论本就不会发送，跳过登记不影响安全
            outcome = await self._skip_comment(attempt, key, reason)
            if isinstance(outcome, ServiceFault):
                return outcome
        return None

    async def _cancel_attempt(
        self, key: DeliveryKey, attempt_id: str, reason: str
    ) -> bool | ServiceFault:
        """撤销一次未消费的准备（账本登记为已撤销，属「明确未发送」）。"""

        try:
            return await self._ledger.cancel_attempt(key, attempt_id=attempt_id, reason=reason)
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"撤销未能落盘（{error.reason}）：按未确认处理，不伪称已撤销",
            )

    def _account_guard(self) -> ServiceFault | None:
        """账号代次守卫：网关账号变化即让旧会话与未消费授权失效并拒绝本次请求。"""

        if self._sessions.note_account_generation(self._gateway.account_generation):
            return ServiceFault(
                HTTP_UNAUTHORIZED,
                ERROR_ACCOUNT_CHANGED,
                "网关实际登录账号已变化：旧会话与未消费授权全部失效，"
                "请重新核对绑定账号后建立新会话（不自动切换账号）",
            )
        return None

    def _stage_view(
        self, session_id: str, *, subject: str, generation: int
    ) -> tuple[SessionView | None, ServiceFault | None]:
        """阶段入口共用的会话与权限复验：返回最新视图或确定的拒绝。"""

        view = self._sessions.view(session_id, subject=subject)
        if isinstance(view, SessionRejection):
            return None, _fault(view)
        if view.policy_generation != generation:
            return None, ServiceFault(
                HTTP_CONFLICT,
                ERROR_STALE_POLICY,
                "请求用的权限代次不是当前代次：旧代次不能触发发送（请使用最新权限代次）",
            )
        if not view.allows_send:
            return None, ServiceFault(
                HTTP_FORBIDDEN, ERROR_PERMISSION_DENIED, "总开关已关闭：拒绝签发或执行任何阶段授权"
            )
        return view, None

    async def _lookup_delivery(self, delivery_id: str) -> DeliverySnapshot | ServiceFault | None:
        """按投递 ID 只读查询；`ServiceFault` 表示账本不可用（与「不存在」区分）。"""

        try:
            return await self._ledger.lookup_delivery(delivery_id)
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"投递状态不可读（{error.reason}）：已关闭发送，不把读取失败当作记录不存在",
            )

    async def _lookup_attempt(self, attempt_id: str) -> AttemptSnapshot | ServiceFault | None:
        """按尝试 ID 只读查询；`ServiceFault` 表示账本不可用（与「不存在」区分）。"""

        try:
            return await self._ledger.lookup_attempt(attempt_id)
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"尝试状态不可读（{error.reason}）：已关闭发送，不把读取失败当作记录不存在",
            )

    async def _register_attempt(
        self, key: DeliveryKey, attempt_id: str, request: PrepareRequest
    ) -> AttemptSnapshot | ServiceFault | None:
        """登记一次「已准备」尝试：只记定位与评论，不产生发送事实。"""

        try:
            return await self._ledger.prepare_attempt(
                key,
                attempt_id=attempt_id,
                request_id=request.request_id,
                session_id=request.session_id,
                policy_generation=request.policy_generation,
                comment=request.comment,
            )
        except LedgerUnavailableError as error:
            return ServiceFault(
                HTTP_SERVICE_UNAVAILABLE,
                ERROR_LEDGER_UNAVAILABLE,
                f"准备未能落盘（{error.reason}）：本次未登记任何可发送状态，拒绝发送",
            )

    # ------------------------------------------------------------------ 准备材料

    def _purge_prepared(self) -> None:
        """释放过期的准备材料（容量与内存有界；过期后不自动发送也不重建）。"""

        now = self._clock.now()
        for attempt_id, stored in list(self._prepared.items()):
            if now > stored.expires_at:
                del self._prepared[attempt_id]

    def _remember_prepared(self, attempt_id: str, delivery_id: str, prepared: PreparedSource) -> None:
        """保留一次准备的临时材料与定位（只在内存里、只在有效期内）。"""

        self._prepared[attempt_id] = _PreparedSource(
            prepared=prepared,
            expires_at=self._clock.now() + PREPARE_CHALLENGE_SECONDS,
        )
        self._attempts_by_delivery[delivery_id] = attempt_id
        if len(self._attempts_by_delivery) > self._max_pending_prepares * 4:
            # 定位表只是内存加速器：超出上限时丢弃最早登记的定位，不影响防重（账本是唯一权威）。
            oldest = next(iter(self._attempts_by_delivery))
            if oldest != delivery_id:
                del self._attempts_by_delivery[oldest]

    def _take_prepared(self, attempt_id: str) -> _PreparedSource | None:
        """取出并释放准备材料（消费一次即释放；过期或已撤销返回 `None`）。"""

        self._purge_prepared()
        return self._prepared.pop(attempt_id, None)
