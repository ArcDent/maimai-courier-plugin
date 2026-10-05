"""服务端后端边界与不发送替身（design D4/D7/D9）。

`ServiceBackends` 是 HTTP 层与真实协调器之间的公开协议：路由只做认证、参数校验与
分发，**不做任何平台发送**。本组提供 `NonSendingBackends`——记录调用、返回契约内
的最小响应、平台发送计数恒为 0；第 3–5 组用真实协调器替换它，路由与测试不变。

后端失败一律用 `ServiceFault` 表达：只带安全错误类别、中文原因与 HTTP 状态，不带
底层响应正文、URL 或凭证（design D5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.protocol import (
    CancelAccepted,
    CancelRequest,
    DeliveryState,
    HeartbeatRequest,
    PREPARE_CHALLENGE_SECONDS,
    PolicyAccepted,
    PolicyUpdateRequest,
    PrepareAccepted,
    PrepareRequest,
    SessionAlive,
    SessionClosed,
    SessionCreateRequest,
    SessionCreated,
    StageExecutionRequest,
    StageExecuted,
    delivery_id_for,
    delivery_key_for,
)


@dataclass(frozen=True, slots=True)
class ServiceFault:
    """后端确定的失败：HTTP 状态 + 错误类别 + 可定位中文原因。"""

    http_status: int
    error: str
    reason: str


class ServiceBackends(Protocol):
    """服务端协调器的公开签名（认证主体 `subject` 由 HTTP 层传入）。"""

    async def create_session(
        self, request: SessionCreateRequest, *, subject: str
    ) -> SessionCreated | ServiceFault: ...

    async def heartbeat(self, session_id: str, request: HeartbeatRequest, *, subject: str) -> SessionAlive | ServiceFault: ...

    async def update_policy(
        self, session_id: str, request: PolicyUpdateRequest, *, subject: str
    ) -> PolicyAccepted | ServiceFault: ...

    async def deregister(self, session_id: str, *, subject: str) -> SessionClosed | ServiceFault: ...

    async def prepare(self, request: PrepareRequest, *, subject: str) -> PrepareAccepted | ServiceFault: ...

    async def execute_source(
        self, delivery_id: str, request: StageExecutionRequest, *, subject: str
    ) -> StageExecuted | ServiceFault: ...

    async def execute_comment(
        self, delivery_id: str, request: StageExecutionRequest, *, subject: str
    ) -> StageExecuted | ServiceFault: ...

    async def query(self, delivery_id: str, *, subject: str) -> DeliveryState | ServiceFault: ...

    async def cancel(
        self, delivery_id: str, request: CancelRequest, *, subject: str
    ) -> CancelAccepted | ServiceFault: ...


@dataclass(slots=True)
class NonSendingBackends:
    """不发送的公开后端替身：验证路由与校验骨架，不做任何平台动作。

    - `calls` 按顺序记录 `(后端方法名, 关键参数)`，供用例断言「合法请求抵达边界」；
    - `platform_send_count` 恒为 0，且本类没有任何发送依赖（不导入网络库、不读凭证）；
    - 准备返回 `prepared`、执行返回明确未发送，绝不报告源投递成功。
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    platform_send_count: int = 0
    account_id: str = "10000"
    ledger_identity: str = "ledger-test"
    server_generation: str = "gen-test"

    def _record(self, name: str, **details: Any) -> None:
        self.calls.append((name, details))

    def _attempt_id(self) -> str:
        return f"attempt-{len(self.calls)}"

    async def create_session(
        self, request: SessionCreateRequest, *, subject: str
    ) -> SessionCreated | ServiceFault:
        self._record("create_session", subject=subject, client_identity=request.client_identity)
        return SessionCreated(
            session_id=f"session-{len(self.calls)}",
            account_id=self.account_id,
            ledger_identity=self.ledger_identity,
            server_generation=self.server_generation,
        )

    async def heartbeat(
        self, session_id: str, request: HeartbeatRequest, *, subject: str
    ) -> SessionAlive | ServiceFault:
        self._record("heartbeat", subject=subject, session_id=session_id)
        return SessionAlive(session_id=session_id)

    async def update_policy(
        self, session_id: str, request: PolicyUpdateRequest, *, subject: str
    ) -> PolicyAccepted | ServiceFault:
        self._record(
            "update_policy",
            subject=subject,
            session_id=session_id,
            policy_generation=request.policy_generation,
        )
        return PolicyAccepted(
            session_id=session_id,
            policy_generation=request.policy_generation,
            enabled=request.enabled,
        )

    async def deregister(self, session_id: str, *, subject: str) -> SessionClosed | ServiceFault:
        self._record("deregister", subject=subject, session_id=session_id)
        return SessionClosed(session_id=session_id)

    async def prepare(self, request: PrepareRequest, *, subject: str) -> PrepareAccepted | ServiceFault:
        key = delivery_key_for(request, account_id=self.account_id)
        attempt_id = self._attempt_id()
        self._record(
            "prepare",
            subject=subject,
            request_id=request.request_id,
            source_message_id=request.source_message_id,
        )
        return PrepareAccepted(
            delivery_id=delivery_id_for(key),
            attempt_id=attempt_id,
            status="prepared",
            challenge=f"challenge-{attempt_id}",
            challenge_expires_in_seconds=PREPARE_CHALLENGE_SECONDS,
        )

    async def execute_source(
        self, delivery_id: str, request: StageExecutionRequest, *, subject: str
    ) -> StageExecuted | ServiceFault:
        self._record(
            "execute_source",
            subject=subject,
            delivery_id=delivery_id,
            attempt_id=request.attempt_id,
        )
        return StageExecuted(
            delivery_id=delivery_id,
            attempt_id=request.attempt_id,
            stage="source",
            status="failed",
            reason="不发送替身没有调用任何平台发送，明确未发送",
        )

    async def execute_comment(
        self, delivery_id: str, request: StageExecutionRequest, *, subject: str
    ) -> StageExecuted | ServiceFault:
        self._record(
            "execute_comment",
            subject=subject,
            delivery_id=delivery_id,
            attempt_id=request.attempt_id,
        )
        return StageExecuted(
            delivery_id=delivery_id,
            attempt_id=request.attempt_id,
            stage="comment",
            status="failed",
            reason="不发送替身没有调用任何平台发送，明确未发送",
        )

    async def query(self, delivery_id: str, *, subject: str) -> DeliveryState | ServiceFault:
        self._record("query", subject=subject, delivery_id=delivery_id)
        return DeliveryState(
            delivery_id=delivery_id,
            status="unknown",
            reason="不发送替身没有持久状态，结果待核实",
        )

    async def cancel(
        self, delivery_id: str, request: CancelRequest, *, subject: str
    ) -> CancelAccepted | ServiceFault:
        self._record(
            "cancel",
            subject=subject,
            delivery_id=delivery_id,
            attempt_id=request.attempt_id,
        )
        return CancelAccepted(
            delivery_id=delivery_id,
            status="cancelled",
            reason="替身没有已消费的阶段授权，按已撤销报告",
        )
