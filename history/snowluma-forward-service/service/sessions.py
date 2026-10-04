"""认证客户端会话、单调权限代次与一次性阶段授权（design D4/D5/D6/D8）。

本模块是服务端的**有限控制状态**：会话、权限代次与阶段授权全部只在内存里短期存在，
不写进账本、不伪装成发送事实。它只回答「谁、在什么权限下、可以提交哪个阶段」，
不做平台发送、不查源、不碰 SQLite。

- 同一认证主体同时只允许一个活动实例：新会话替换旧会话，旧实例未消费的授权立即失效；
- 心跳只续期（`SESSION_TIMEOUT_SECONDS`），不更新权限、不激活准备或发送；
- 权限代次单调递增：旧代次不能覆盖新代次；有效权限是「客户端许可 ∩ 服务端上限」；
- 阶段授权（挑战）一次性：只对当前活动且未失效的会话签发，绑定投递、尝试、会话、
  权限代次、阶段与到期时间，不能互用；消费后即从表中移除，重复执行请求由账本里的
  原尝试状态回答（不重开新尝试）；
- 所有时间判断都走注入时钟；容量有界，超限明确拒绝（不静默逐出未过期挑战）。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final, Iterable

from shared.clock import Clock
from shared.protocol import (
    ALL_STAGES,
    ERROR_EXPIRED_AUTHORIZATION,
    HEARTBEAT_INTERVAL_SECONDS,
    PREPARE_CHALLENGE_SECONDS,
    SESSION_TIMEOUT_SECONDS,
    PolicyAccepted,
    SessionAlive,
    SessionCreated,
    Stage,
    StageAuthorization,
    authorization_rejection,
)

from .http_api import (
    HTTP_CONFLICT,
    HTTP_FORBIDDEN,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
)

MAX_PENDING_AUTHORIZATIONS: Final[int] = 256
"""未消费阶段授权（挑战）的数量上限（代码常量）：超限明确繁忙，不静默逐出有效挑战。"""

ERROR_SESSION_INVALID: Final[str] = "session_invalid"
"""会话不存在、已过期、已被新实例替换或不属于当前主体。"""

ERROR_STALE_POLICY: Final[str] = "stale_policy"
"""权限代次不是当前代次或不是严格递增（旧代次不能覆盖新代次）。"""

ERROR_PERMISSION_DENIED: Final[str] = "permission_denied"
"""总开关关闭或请求的源/目标不在有效许可（客户端许可 ∩ 服务端上限）内。"""

ERROR_AUTHORIZATION_UNKNOWN: Final[str] = "authorization_unknown"
"""阶段授权不存在、已被消费或已失效（不自动重建，也不排队重发）。"""

ERROR_AUTHORIZATION_CAPACITY: Final[str] = "authorization_capacity"
"""未消费授权已达容量上限：明确繁忙，不驱逐仍在有效期内的挑战。"""


@dataclass(frozen=True, slots=True)
class SessionRejection:
    """控制状态拒绝：安全错误类别 + 可定位中文原因 + HTTP 状态（都不含正文与凭证）。"""

    error: str
    reason: str
    http_status: int = HTTP_FORBIDDEN


@dataclass(frozen=True, slots=True)
class SessionView:
    """会话的有效视图：身份 + 权限代次 + 客户端许可与服务端上限的交集。"""

    session_id: str
    subject: str
    policy_generation: int
    enabled: bool
    source_groups: frozenset[str]
    target_groups: frozenset[str]
    expires_at: float

    @property
    def allows_send(self) -> bool:
        """总开关是否打开（关掉总开关后一切阶段都不得签发或执行）。"""

        return self.enabled

    def allows_source(self, group_id: str) -> bool:
        return self.enabled and group_id in self.source_groups

    def allows_target(self, group_id: str) -> bool:
        return self.enabled and group_id in self.target_groups


@dataclass(frozen=True, slots=True)
class ClosedSession:
    """会话注销结果：被撤销的未消费授权（评论挑战需据此登记跳过）。"""

    session_id: str
    revoked: tuple[StageAuthorization, ...]


@dataclass(slots=True)
class _SessionRecord:
    """会话内部记录：客户端许可保持原样，服务端上限在视图里取交集。"""

    session_id: str
    subject: str
    policy_generation: int
    enabled: bool
    source_groups: frozenset[str]
    target_groups: frozenset[str]
    expires_at: float


class SessionRegistry:
    """会话、权限代次与一次性阶段授权的唯一持有者（内存、有界、可注入时钟）。"""

    def __init__(
        self,
        *,
        clock: Clock,
        account_id: str,
        ledger_identity: str,
        server_generation: str,
        server_source_groups: Iterable[str] = (),
        server_target_groups: Iterable[str] = (),
        account_generation: int = 0,
        max_authorizations: int = MAX_PENDING_AUTHORIZATIONS,
    ) -> None:
        self._clock = clock
        self._account_id = account_id
        self._ledger_identity = ledger_identity
        self._server_generation = server_generation
        self._server_source_groups = frozenset(server_source_groups)
        self._server_target_groups = frozenset(server_target_groups)
        self._account_generation = int(account_generation)
        self._max_authorizations = int(max_authorizations)
        self._sessions: dict[str, _SessionRecord] = {}
        self._subject_sessions: dict[str, str] = {}
        self._authorizations: dict[str, StageAuthorization] = {}

    # ------------------------------------------------------------------ 只读面

    @property
    def account_id(self) -> str:
        """绑定账号（只来自受控配置）。"""

        return self._account_id

    @property
    def ledger_identity(self) -> str:
        """账本身份（会话建立时回给客户端，误连空库靠它识别）。"""

        return self._ledger_identity

    @property
    def server_generation(self) -> str:
        """服务启动代次：每次进程启动都不同，旧代次的会话与挑战不能继续使用。"""

        return self._server_generation

    @property
    def account_generation(self) -> int:
        """已记录的网关账号代次。"""

        return self._account_generation

    @property
    def pending_authorizations(self) -> int:
        """当前未消费且未过期的阶段授权数量（容量可观测面；先清理过期授权）。"""

        self.purge_expired()
        return len(self._authorizations)

    def active_session_for(self, subject: str) -> str:
        """该主体当前的活动会话 ID；没有返回空串。"""

        record = self._sessions.get(self._subject_sessions.get(subject, ""))
        return record.session_id if record is not None else ""

    def session_subject(self, session_id: str) -> str:
        """会话对应的认证主体；会话不存在返回空串（重启后旧会话一律未知）。"""

        record = self._sessions.get(session_id)
        return record.subject if record is not None else ""

    # ------------------------------------------------------------------ 账号与失效

    def note_account_generation(self, generation: int) -> bool:
        """记录网关账号代次；代次变化即让全部会话与未消费授权失效（返回是否变化）。"""

        if generation == self._account_generation:
            return False
        self._account_generation = int(generation)
        self.invalidate_all()
        return True

    def invalidate_all(self) -> tuple[StageAuthorization, ...]:
        """失效全部会话与未消费授权（换账号、重启等场景）；返回被撤销的授权。"""

        revoked = tuple(self._authorizations.values())
        self._authorizations.clear()
        self._sessions.clear()
        self._subject_sessions.clear()
        return revoked

    def purge_expired(self) -> int:
        """清理过期会话与过期授权（有界内存的例行回收），返回清理条数。"""

        now = self._clock.now()
        expired_sessions = [
            session_id
            for session_id, record in self._sessions.items()
            if now > record.expires_at
        ]
        for session_id in expired_sessions:
            self._drop_session(session_id)
        expired_tokens = [
            token
            for token, authorization in self._authorizations.items()
            if now > authorization.expires_at
        ]
        for token in expired_tokens:
            del self._authorizations[token]
        return len(expired_sessions) + len(expired_tokens)

    # ------------------------------------------------------------------ 会话

    def open(self, subject: str, client_identity: str) -> SessionCreated:
        """建立会话：同一主体只保留一个活动实例，旧实例连同其未消费授权一起失效。

        `client_identity` 只用于调用方核对客户端身份（协调器比对受控配置），不落任何
        状态：会话记录只保留主体、代次、许可与到期时间。
        """

        self.purge_expired()
        previous = self._subject_sessions.get(subject, "")
        if previous:
            self._drop_session(previous)
        session_id = f"s-{secrets.token_urlsafe(18)}"
        self._sessions[session_id] = _SessionRecord(
            session_id=session_id,
            subject=subject,
            policy_generation=0,
            enabled=False,
            source_groups=frozenset(),
            target_groups=frozenset(),
            expires_at=self._clock.now() + SESSION_TIMEOUT_SECONDS,
        )
        self._subject_sessions[subject] = session_id
        return SessionCreated(
            session_id=session_id,
            account_id=self._account_id,
            ledger_identity=self._ledger_identity,
            server_generation=self._server_generation,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
        )

    def heartbeat(self, session_id: str, *, subject: str) -> SessionAlive | SessionRejection:
        """心跳只续期：不更新权限、不激活准备或发送。"""

        record = self._live(session_id, subject)
        if isinstance(record, SessionRejection):
            return record
        record.expires_at = self._clock.now() + SESSION_TIMEOUT_SECONDS
        return SessionAlive(session_id=session_id, expires_in_seconds=SESSION_TIMEOUT_SECONDS)

    def update_policy(
        self,
        session_id: str,
        *,
        subject: str,
        generation: int,
        enabled: bool,
        source_groups: Iterable[str],
        target_groups: Iterable[str],
    ) -> PolicyAccepted | SessionRejection:
        """更新权限代次：必须严格递增；旧代次不能覆盖新代次（拒绝，不改动现状）。"""

        record = self._live(session_id, subject)
        if isinstance(record, SessionRejection):
            return record
        if generation <= record.policy_generation:
            return SessionRejection(
                ERROR_STALE_POLICY,
                "权限代次必须严格递增：旧代次不能覆盖新代次，本次未改动任何许可",
                HTTP_CONFLICT,
            )
        record.policy_generation = int(generation)
        record.enabled = bool(enabled)
        record.source_groups = frozenset(source_groups)
        record.target_groups = frozenset(target_groups)
        return PolicyAccepted(session_id=session_id, policy_generation=generation, enabled=enabled)

    def close(self, session_id: str, *, subject: str) -> ClosedSession | SessionRejection:
        """注销会话并撤销其全部未消费授权；已保存的投递回执不在这里删除。"""

        record = self._live(session_id, subject)
        if isinstance(record, SessionRejection):
            return record
        revoked = self._revoke_session(session_id)
        self._drop_session(session_id)
        return ClosedSession(session_id=session_id, revoked=revoked)

    def view(self, session_id: str, *, subject: str) -> SessionView | SessionRejection:
        """取会话的有效视图；不存在、过期、不属于当前主体一律拒绝。"""

        record = self._live(session_id, subject)
        if isinstance(record, SessionRejection):
            return record
        server_sources = self._server_source_groups
        server_targets = self._server_target_groups
        return SessionView(
            session_id=record.session_id,
            subject=record.subject,
            policy_generation=record.policy_generation,
            enabled=record.enabled,
            source_groups=frozenset(record.source_groups & server_sources),
            target_groups=frozenset(record.target_groups & server_targets),
            expires_at=record.expires_at,
        )

    # ------------------------------------------------------------------ 阶段授权

    def issue(
        self,
        *,
        stage: Stage,
        delivery_id: str,
        attempt_id: str,
        session_id: str,
        policy_generation: int,
        ttl: float = PREPARE_CHALLENGE_SECONDS,
    ) -> StageAuthorization | SessionRejection:
        """签发一次性阶段授权（挑战）：绑定投递、尝试、会话、代次、阶段与到期时间。

        只对**当前活动且未失效**的会话签发：会话被注销、被同一主体的新实例替换或已过期
        时直接拒绝，不签发绑定死会话的挑战（客户端不会拿到不可用 token，也不占容量至
        过期）。签发不发送任何消息，也不读取平台状态。
        """

        if stage not in ALL_STAGES or not delivery_id or not attempt_id or not session_id:
            return SessionRejection(
                ERROR_SESSION_INVALID, "阶段授权的绑定信息不完整，拒绝签发"
            )
        record = self._sessions.get(session_id)
        if record is None or self._subject_sessions.get(record.subject) != session_id:
            return SessionRejection(
                ERROR_SESSION_INVALID,
                "会话已注销、已失效或已被同一主体的新实例替换：拒绝签发绑定死会话的阶段授权",
                HTTP_UNAUTHORIZED,
            )
        if self._clock.now() > record.expires_at:
            self._drop_session(session_id)
            return SessionRejection(
                ERROR_SESSION_INVALID,
                "会话已超过失联到期：拒绝签发绑定死会话的阶段授权",
                HTTP_UNAUTHORIZED,
            )
        self.purge_expired()
        if len(self._authorizations) >= self._max_authorizations:
            return SessionRejection(
                ERROR_AUTHORIZATION_CAPACITY,
                "未消费的阶段授权已达容量上限，明确繁忙（不驱逐仍在有效期内的挑战）",
                HTTP_TOO_MANY_REQUESTS,
            )
        authorization = StageAuthorization(
            stage=stage,
            token=f"a-{secrets.token_urlsafe(24)}",
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            session_id=session_id,
            policy_generation=int(policy_generation),
            expires_at=self._clock.now() + float(ttl),
        )
        self._authorizations[authorization.token] = authorization
        return authorization

    def consume(
        self,
        token: str,
        *,
        expected_stage: Stage,
        delivery_id: str,
        attempt_id: str,
        session_id: str,
        policy_generation: int,
    ) -> StageAuthorization | SessionRejection:
        """消费一次性阶段授权：阶段/绑定/过期校验通过才消费，重复消费一律拒绝。"""

        authorization = self._authorizations.get(token)
        if authorization is None:
            return SessionRejection(
                ERROR_AUTHORIZATION_UNKNOWN,
                "阶段授权不存在、已被消费或已失效；不自动重建，也不排队重发",
                HTTP_CONFLICT,
            )
        rejection = authorization_rejection(
            authorization,
            expected_stage=expected_stage,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            session_id=session_id,
            policy_generation=int(policy_generation),
            now=self._clock.now(),
        )
        if rejection is not None:
            if rejection.error == ERROR_EXPIRED_AUTHORIZATION:
                del self._authorizations[token]
            return SessionRejection(rejection.error, rejection.reason, HTTP_CONFLICT)
        del self._authorizations[token]
        return authorization

    def find(
        self, *, delivery_id: str, attempt_id: str, stage: Stage
    ) -> StageAuthorization | None:
        """查未消费且未过期的阶段授权（只读；用于向原调用续接评论挑战）。"""

        self.purge_expired()
        for authorization in self._authorizations.values():
            if (
                authorization.delivery_id == delivery_id
                and authorization.attempt_id == attempt_id
                and authorization.stage == stage
            ):
                return authorization
        return None

    def revoke_delivery(self, delivery_id: str, *, stage: Stage | None = None) -> tuple[StageAuthorization, ...]:
        """撤销某投递的未消费授权（可选只撤销某个阶段），返回被撤销的授权。"""

        revoked: list[StageAuthorization] = []
        for token, authorization in list(self._authorizations.items()):
            if authorization.delivery_id != delivery_id:
                continue
            if stage is not None and authorization.stage != stage:
                continue
            revoked.append(authorization)
            del self._authorizations[token]
        return tuple(revoked)

    # ------------------------------------------------------------------ 内部

    def _live(self, session_id: str, subject: str) -> _SessionRecord | SessionRejection:
        """取可用会话记录：不存在、不属于当前主体或已过期一律拒绝（不自动恢复）。"""

        record = self._sessions.get(session_id)
        if record is None:
            return SessionRejection(
                ERROR_SESSION_INVALID,
                "会话不存在、已失效或已被新实例替换；请重新建立会话",
                HTTP_UNAUTHORIZED,
            )
        if record.subject != subject:
            return SessionRejection(
                ERROR_SESSION_INVALID, "会话不属于当前认证主体，拒绝访问", HTTP_FORBIDDEN
            )
        if self._clock.now() > record.expires_at:
            self._drop_session(session_id)
            return SessionRejection(
                ERROR_SESSION_INVALID,
                "会话已超过失联到期（心跳只续期，不自动恢复）",
                HTTP_UNAUTHORIZED,
            )
        return record

    def _drop_session(self, session_id: str) -> tuple[StageAuthorization, ...]:
        """移除会话与其未消费授权（同时清理主体映射）。"""

        record = self._sessions.pop(session_id, None)
        if record is not None and self._subject_sessions.get(record.subject) == session_id:
            del self._subject_sessions[record.subject]
        return self._revoke_session(session_id)

    def _revoke_session(self, session_id: str) -> tuple[StageAuthorization, ...]:
        """撤销某会话的全部未消费授权。"""

        revoked: list[StageAuthorization] = []
        for token, authorization in list(self._authorizations.items()):
            if authorization.session_id != session_id:
                continue
            revoked.append(authorization)
            del self._authorizations[token]
        return tuple(revoked)
