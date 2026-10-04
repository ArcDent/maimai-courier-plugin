"""服务端 v1 协议契约：请求/响应模型、字段校验与阶段授权（design D4/D5/D7/D8）。

本模块只放纯数据与纯校验：不发送、不落盘、不连网，也不导入插件侧模块或 SDK。

模型职责（互不混用，都不是防重键）：

- **请求 ID**（`request_id`）：一次客户端请求的定位符，可重复，不参与防重；
- **投递 ID**（`delivery_id`）：`DeliveryKey` 四字段的稳定摘要，是查询与撤销的对外
  句柄；防重身份仍是四字段本身（`delivery_id_for` 只做摘要）；
- **会话 ID**（`session_id`）：认证客户端的单个活动运行实例；
- **权限代次**（`policy_generation`）：单调递增的发送许可版本，旧代次不能覆盖新代次；
- **阶段**（`Stage`）：`prepare` / `source` / `comment`，一次授权只属于一个阶段。

请求体一律拒绝未知字段（包括任何试图从请求覆盖账号、状态目录、监听地址或许可上限
的字段）；标识符统一走 `shared.identifiers.safe_identifier`，非法值直接拒绝、不净化。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping, get_args

from .delivery import DeliveryKey, LedgerStatus, SendStatus
from .identifiers import MAX_IDENTIFIER_LENGTH, verbatim_identifier

PROTOCOL_VERSION: Final[int] = 1
"""HTTP/JSON 协议版本（design D4：v1）；请求必须显式声明且必须等于本值。"""

PROTOCOL_VERSION_FIELD: Final[str] = "protocol_version"
"""协议版本字段名（唯一允许声明版本的入口）。"""

MAX_COMMENT_LENGTH: Final[int] = 2000
"""评论字段长度上限（design D5 要求请求字段有界；数值属实现，后续可依负载调整）。"""

HEARTBEAT_INTERVAL_SECONDS: Final[float] = 5.0
"""客户端心跳间隔（秒）：代码常量，不做成用户配置（design D4）。"""

SESSION_TIMEOUT_SECONDS: Final[float] = 15.0
"""会话失联到期（秒）：代码常量，心跳只续期（design D4）。"""

PREPARE_CHALLENGE_SECONDS: Final[float] = 30.0
"""准备/阶段授权的有效窗口（秒）：代码常量（design D4）。"""

STAGE_EXECUTION_WINDOW_SECONDS: Final[float] = 2.0
"""已提交阶段授权可开始发送的有效窗口（秒）：代码常量（design D4）。"""

Stage = Literal["prepare", "source", "comment"]
"""阶段：准备 / 源发送 / 评论发送；授权与挑战不能跨阶段互用。"""

STAGE_PREPARE: Final[Stage] = "prepare"
STAGE_SOURCE: Final[Stage] = "source"
STAGE_COMMENT: Final[Stage] = "comment"
ALL_STAGES: Final[tuple[Stage, ...]] = ("prepare", "source", "comment")

CommentStatus = Literal["not_requested", "pending", "sending", "sent", "failed", "unknown", "skipped"]
"""评论阶段状态（design D7 封闭值域）：未请求 / 待执行 / 在途 / 已发送 / 明确未发 / 待核实 / 跳过。"""

COMMENT_STATUS_VALUES: Final[frozenset[str]] = frozenset(get_args(CommentStatus))

PrepareStatus = Literal["prepared", "already_forwarded", "sending", "unknown"]
"""准备结果：已准备 / 既有成功阻断 / 在途阻断 / 既有待核实阻断（都不发送）。"""

PREPARE_STATUS_VALUES: Final[frozenset[str]] = frozenset(get_args(PrepareStatus))

CancelStatus = Literal["cancelled", "in_flight", "completed", "not_found"]
"""撤销结果：已撤销未消费授权 / 已开始 / 已完成 / 无此投递（design D6）。"""

CANCEL_STATUS_VALUES: Final[frozenset[str]] = frozenset(get_args(CancelStatus))

ERROR_UNKNOWN_FIELD: Final[str] = "unknown_field"
"""请求包含未知字段（含试图覆盖受控配置的字段）。"""

ERROR_MISSING_FIELD: Final[str] = "missing_field"
"""缺少必填字段。"""

ERROR_INVALID_ID: Final[str] = "invalid_id"
"""标识符非法（空、超长、空白/控制字符或禁止字符），不净化。"""

ERROR_INVALID_FIELD: Final[str] = "invalid_field"
"""字段类型或取值非法（如评论非字符串、超长）。"""

ERROR_PROTOCOL_VERSION: Final[str] = "protocol_version"
"""协议版本缺失或不是受支持的 v1。"""

ERROR_STAGE_MISMATCH: Final[str] = "stage_mismatch"
"""授权阶段与本次执行阶段不符（源授权不能发评论，反之亦然）。"""

ERROR_AUTHORIZATION_MISMATCH: Final[str] = "authorization_mismatch"
"""授权绑定的投递、尝试、会话或权限代次与本次请求不符。"""

ERROR_EXPIRED_AUTHORIZATION: Final[str] = "expired_authorization"
"""授权已过期（过期不自动延期、不排队重发）。"""

ALLOW_REDIRECTS: Final[bool] = False
"""客户端是否允许跟随 HTTP 重定向：固定 False（认证头不得随重定向转交，design D5）。

服务端自身也不产生任何 3xx：认证失败、路径不存在与方法不符分别以 401 / 404 / 405
直接回答，不给重定向留下转交认证信息的机会。
"""


@dataclass(frozen=True, slots=True)
class InvalidRequest:
    """请求被拒绝：带机器可读类别与可定位中文原因（不含请求正文）。"""

    error: str
    reason: str


# --------------------------------------------------------------------------- 身份


def delivery_id_for(key: DeliveryKey) -> str:
    """投递 ID：四字段规范编码的稳定摘要（请求 ID、会话、代次都不参与）。

    分隔符用 `\\x1f`（单元分隔符）：标识符规则禁止控制字符，因此四字段拼接无歧义；
    摘要是十六进制文本，满足标识符长度上限，可安全出现在请求路径与日志里。
    """

    canonical = "\x1f".join(
        (key.account_id, key.source_group_id, key.source_message_id, key.target_group_id)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def delivery_key_for(request: "PrepareRequest", *, account_id: str) -> DeliveryKey:
    """准备请求 + 受控账号 → 投递四字段身份。

    账号只来自服务端受控配置（会话绑定的账号），**不接受请求覆盖**；请求只提供
    源群、源消息与目标群。
    """

    return DeliveryKey(
        account_id=account_id,
        source_group_id=request.source_group_id,
        source_message_id=request.source_message_id,
        target_group_id=request.target_group_id,
    )


# --------------------------------------------------------------------------- 请求


@dataclass(frozen=True, slots=True)
class SessionCreateRequest:
    """建立认证会话：只声明客户端身份名（凭证由请求头携带）。"""

    client_identity: str
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class HeartbeatRequest:
    """心跳：只续期，不更新权限、不激活准备或发送。"""

    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class PolicyUpdateRequest:
    """权限代次更新：单调递增代次 + 总开关 + 客户端许可集合。"""

    policy_generation: int
    enabled: bool
    source_groups: tuple[str, ...] = ()
    target_groups: tuple[str, ...] = ()
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    """准备请求：请求 ID + 会话/权限代次 + 投递三字段 + 本次评论（不发送）。"""

    request_id: str
    session_id: str
    policy_generation: int
    source_group_id: str
    source_message_id: str
    target_group_id: str
    comment: str = ""
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class StageExecutionRequest:
    """源/评论阶段执行：尝试 + 会话 + 权限代次 + 该阶段的单次挑战。"""

    attempt_id: str
    session_id: str
    policy_generation: int
    challenge: str
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class CancelRequest:
    """撤销请求：可选指定要撤销的尝试（缺省撤销该投递全部未消费授权）。"""

    attempt_id: str = ""
    protocol_version: int = PROTOCOL_VERSION


# --------------------------------------------------------------------------- 授权


@dataclass(frozen=True, slots=True)
class StageAuthorization:
    """服务端签发的单次阶段授权：绑定投递、尝试、会话、权限代次、阶段与到期时间。

    挑战正文本身是随机令牌，本模型只描述绑定关系；撤销/失效由服务端登记的表决定。
    """

    stage: Stage
    token: str
    delivery_id: str
    attempt_id: str
    session_id: str
    policy_generation: int
    expires_at: float


def authorization_rejection(
    authorization: StageAuthorization,
    *,
    expected_stage: Stage,
    delivery_id: str,
    attempt_id: str,
    session_id: str,
    policy_generation: int,
    now: float,
) -> InvalidRequest | None:
    """校验一次阶段授权：通过返回 `None`，否则给出确定的拒绝类别。

    顺序固定为：阶段 → 绑定（投递/尝试/会话/代次）→ 过期。阶段不符优先于绑定不符，
    保证「用源授权发评论」与「换尝试重用授权」两种错用可区分。
    """

    if authorization.stage != expected_stage:
        return InvalidRequest(
            error=ERROR_STAGE_MISMATCH,
            reason=f"授权属于 {authorization.stage} 阶段，不能用于 {expected_stage} 阶段",
        )
    if (
        authorization.delivery_id != delivery_id
        or authorization.attempt_id != attempt_id
        or authorization.session_id != session_id
        or authorization.policy_generation != policy_generation
    ):
        return InvalidRequest(
            error=ERROR_AUTHORIZATION_MISMATCH,
            reason="授权绑定的投递、尝试、会话或权限代次与本次请求不符",
        )
    if now > authorization.expires_at:
        return InvalidRequest(
            error=ERROR_EXPIRED_AUTHORIZATION,
            reason="授权已过期，不自动延期也不排队重发",
        )
    return None


# --------------------------------------------------------------------------- 响应


@dataclass(frozen=True, slots=True)
class SessionCreated:
    """会话建立成功：绑定账号、账本身份与服务启动代次（design D4）。"""

    session_id: str
    account_id: str
    ledger_identity: str
    server_generation: str
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS
    session_timeout_seconds: float = SESSION_TIMEOUT_SECONDS
    protocol_version: int = PROTOCOL_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "account_id": self.account_id,
            "ledger_identity": self.ledger_identity,
            "server_generation": self.server_generation,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "session_timeout_seconds": self.session_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class SessionAlive:
    """心跳只续期：返回剩余有效期，不改变权限。"""

    session_id: str
    expires_in_seconds: float = SESSION_TIMEOUT_SECONDS

    def as_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "expires_in_seconds": self.expires_in_seconds,
        }


@dataclass(frozen=True, slots=True)
class PolicyAccepted:
    """权限代次更新被接受：回显新代次与总开关状态。"""

    session_id: str
    policy_generation: int
    enabled: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "policy_generation": self.policy_generation,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class SessionClosed:
    """会话注销：未消费阶段授权同时失效，已保存回执保留。"""

    session_id: str

    def as_payload(self) -> dict[str, Any]:
        return {"session_id": self.session_id}


@dataclass(frozen=True, slots=True)
class PrepareAccepted:
    """准备结果：不发送；返回投递/尝试、既有阻断状态与源阶段挑战。"""

    delivery_id: str
    attempt_id: str
    status: str
    challenge: str = ""
    challenge_expires_in_seconds: float = 0.0
    target_message_id: str = ""
    comment_status: str = "not_requested"

    def as_payload(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "challenge": self.challenge,
            "challenge_expires_in_seconds": self.challenge_expires_in_seconds,
            "target_message_id": self.target_message_id,
            "comment_status": self.comment_status,
        }


@dataclass(frozen=True, slots=True)
class StageExecuted:
    """源/评论阶段执行结果：权威状态 + 已知目标 ID（源成功时附评论挑战）。"""

    delivery_id: str
    attempt_id: str
    stage: Stage
    status: SendStatus
    target_message_id: str = ""
    comment_status: str = "not_requested"
    comment_message_id: str = ""
    comment_challenge: str = ""
    comment_challenge_expires_in_seconds: float = 0.0
    reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "attempt_id": self.attempt_id,
            "stage": self.stage,
            "status": self.status,
            "target_message_id": self.target_message_id,
            "comment_status": self.comment_status,
            "comment_message_id": self.comment_message_id,
            "comment_challenge": self.comment_challenge,
            "comment_challenge_expires_in_seconds": self.comment_challenge_expires_in_seconds,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DeliveryState:
    """只读查询结果：权威源/评论状态与已知目标 ID，不含任何正文。"""

    delivery_id: str
    status: LedgerStatus
    target_message_id: str = ""
    comment_status: str = "not_requested"
    comment_message_id: str = ""
    comment_challenge: str = ""
    comment_challenge_expires_in_seconds: float = 0.0
    reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "status": self.status,
            "target_message_id": self.target_message_id,
            "comment_status": self.comment_status,
            "comment_message_id": self.comment_message_id,
            "comment_challenge": self.comment_challenge,
            "comment_challenge_expires_in_seconds": self.comment_challenge_expires_in_seconds,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CancelAccepted:
    """撤销结果：已撤销 / 已开始 / 已完成 / 无此投递（不伪称已停止在途发送）。"""

    delivery_id: str
    status: str
    reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {"delivery_id": self.delivery_id, "status": self.status, "reason": self.reason}


# --------------------------------------------------------------------------- 校验


def _as_mapping(payload: Any) -> Mapping[str, Any] | InvalidRequest:
    """请求体必须是 JSON 对象（映射）；其余形态一律拒绝。"""

    if not isinstance(payload, Mapping):
        return InvalidRequest(error=ERROR_INVALID_FIELD, reason="请求体必须是 JSON 对象")
    return payload


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str]) -> InvalidRequest | None:
    """拒绝任何未知字段（含试图覆盖受控配置的字段名）。"""

    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        return InvalidRequest(
            error=ERROR_UNKNOWN_FIELD,
            reason=f"请求包含未知字段：{', '.join(unknown)}",
        )
    return None


def _parse_version(payload: Mapping[str, Any]) -> int | InvalidRequest:
    """协议版本必须存在且等于 v1。"""

    if PROTOCOL_VERSION_FIELD not in payload:
        return InvalidRequest(error=ERROR_MISSING_FIELD, reason=f"缺少字段 {PROTOCOL_VERSION_FIELD}")
    value = payload[PROTOCOL_VERSION_FIELD]
    if not isinstance(value, int) or isinstance(value, bool) or value != PROTOCOL_VERSION:
        return InvalidRequest(
            error=ERROR_PROTOCOL_VERSION,
            reason=f"只支持协议版本 {PROTOCOL_VERSION}",
        )
    return value


def _parse_identifier(payload: Mapping[str, Any], name: str) -> str | InvalidRequest:
    """标识符字段：缺失与非法可区分，非法一律不净化。"""

    if name not in payload:
        return InvalidRequest(error=ERROR_MISSING_FIELD, reason=f"缺少字段 {name}")
    value = verbatim_identifier(payload[name])
    if not value:
        return InvalidRequest(
            error=ERROR_INVALID_ID,
            reason=f"字段 {name} 不是安全可引用的标识符（非空、≤{MAX_IDENTIFIER_LENGTH} 字符、无空白/控制字符/尖括号/引号/反引号）",
        )
    return value


def _parse_generation(payload: Mapping[str, Any], name: str) -> int | InvalidRequest:
    """权限代次：非负整数（布尔不是整数）。"""

    if name not in payload:
        return InvalidRequest(error=ERROR_MISSING_FIELD, reason=f"缺少字段 {name}")
    value = payload[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return InvalidRequest(error=ERROR_INVALID_FIELD, reason=f"字段 {name} 必须是非负整数")
    return value


def _parse_bool(payload: Mapping[str, Any], name: str) -> bool | InvalidRequest:
    """布尔字段必须是真的布尔（不接受字符串化）。"""

    if name not in payload:
        return InvalidRequest(error=ERROR_MISSING_FIELD, reason=f"缺少字段 {name}")
    value = payload[name]
    if not isinstance(value, bool):
        return InvalidRequest(error=ERROR_INVALID_FIELD, reason=f"字段 {name} 必须是布尔值")
    return value


def _parse_comment(payload: Mapping[str, Any]) -> str | InvalidRequest:
    """评论可选；必须是字符串且不超上限（空串合法：只转发、不评论）。"""

    if "comment" not in payload:
        return ""
    value = payload["comment"]
    if not isinstance(value, str):
        return InvalidRequest(error=ERROR_INVALID_FIELD, reason="字段 comment 必须是字符串")
    if len(value) > MAX_COMMENT_LENGTH:
        return InvalidRequest(
            error=ERROR_INVALID_FIELD,
            reason=f"字段 comment 超过 {MAX_COMMENT_LENGTH} 字符上限",
        )
    return value


def _parse_identifier_tuple(payload: Mapping[str, Any], name: str) -> tuple[str, ...] | InvalidRequest:
    """群号集合：必须是列表且每项都是安全标识符（保持顺序、允许为空集合）。"""

    if name not in payload:
        return ()
    value = payload[name]
    if not isinstance(value, (list, tuple)):
        return InvalidRequest(error=ERROR_INVALID_FIELD, reason=f"字段 {name} 必须是数组")
    parsed: list[str] = []
    for item in value:
        identifier = verbatim_identifier(item)
        if not identifier:
            return InvalidRequest(
                error=ERROR_INVALID_ID,
                reason=f"字段 {name} 含不是安全标识符的群号",
            )
        parsed.append(identifier)
    return tuple(parsed)


def parse_session_create(payload: Any) -> SessionCreateRequest | InvalidRequest:
    """解析 `POST /v1/sessions` 请求体。"""

    mapping = _as_mapping(payload)
    if isinstance(mapping, InvalidRequest):
        return mapping
    allowed = frozenset({PROTOCOL_VERSION_FIELD, "client_identity"})
    rejected = _reject_unknown(mapping, allowed)
    if rejected is not None:
        return rejected
    version = _parse_version(mapping)
    if isinstance(version, InvalidRequest):
        return version
    identity = _parse_identifier(mapping, "client_identity")
    if isinstance(identity, InvalidRequest):
        return identity
    return SessionCreateRequest(client_identity=identity, protocol_version=version)


def parse_heartbeat_request(payload: Any) -> HeartbeatRequest | InvalidRequest:
    """解析心跳请求体（只允许协议版本字段）。"""

    mapping = _as_mapping(payload)
    if isinstance(mapping, InvalidRequest):
        return mapping
    rejected = _reject_unknown(mapping, frozenset({PROTOCOL_VERSION_FIELD}))
    if rejected is not None:
        return rejected
    version = _parse_version(mapping)
    if isinstance(version, InvalidRequest):
        return version
    return HeartbeatRequest(protocol_version=version)


def parse_policy_update_request(payload: Any) -> PolicyUpdateRequest | InvalidRequest:
    """解析 `PUT /v1/sessions/{session_id}/policy` 请求体。"""

    mapping = _as_mapping(payload)
    if isinstance(mapping, InvalidRequest):
        return mapping
    allowed = frozenset(
        {PROTOCOL_VERSION_FIELD, "policy_generation", "enabled", "source_groups", "target_groups"}
    )
    rejected = _reject_unknown(mapping, allowed)
    if rejected is not None:
        return rejected
    version = _parse_version(mapping)
    if isinstance(version, InvalidRequest):
        return version
    generation = _parse_generation(mapping, "policy_generation")
    if isinstance(generation, InvalidRequest):
        return generation
    enabled = _parse_bool(mapping, "enabled")
    if isinstance(enabled, InvalidRequest):
        return enabled
    source_groups = _parse_identifier_tuple(mapping, "source_groups")
    if isinstance(source_groups, InvalidRequest):
        return source_groups
    target_groups = _parse_identifier_tuple(mapping, "target_groups")
    if isinstance(target_groups, InvalidRequest):
        return target_groups
    return PolicyUpdateRequest(
        policy_generation=generation,
        enabled=enabled,
        source_groups=source_groups,
        target_groups=target_groups,
        protocol_version=version,
    )


def parse_prepare_request(payload: Any) -> PrepareRequest | InvalidRequest:
    """解析 `POST /v1/forwards/prepare` 请求体。

    只接受请求 ID、会话/权限代次、投递三字段与评论；账号、状态目录、许可上限或
    监听地址等受控配置字段一律按未知字段拒绝。
    """

    mapping = _as_mapping(payload)
    if isinstance(mapping, InvalidRequest):
        return mapping
    allowed = frozenset(
        {
            PROTOCOL_VERSION_FIELD,
            "request_id",
            "session_id",
            "policy_generation",
            "source_group_id",
            "source_message_id",
            "target_group_id",
            "comment",
        }
    )
    rejected = _reject_unknown(mapping, allowed)
    if rejected is not None:
        return rejected
    version = _parse_version(mapping)
    if isinstance(version, InvalidRequest):
        return version
    request_id = _parse_identifier(mapping, "request_id")
    if isinstance(request_id, InvalidRequest):
        return request_id
    session_id = _parse_identifier(mapping, "session_id")
    if isinstance(session_id, InvalidRequest):
        return session_id
    generation = _parse_generation(mapping, "policy_generation")
    if isinstance(generation, InvalidRequest):
        return generation
    source_group_id = _parse_identifier(mapping, "source_group_id")
    if isinstance(source_group_id, InvalidRequest):
        return source_group_id
    source_message_id = _parse_identifier(mapping, "source_message_id")
    if isinstance(source_message_id, InvalidRequest):
        return source_message_id
    target_group_id = _parse_identifier(mapping, "target_group_id")
    if isinstance(target_group_id, InvalidRequest):
        return target_group_id
    comment = _parse_comment(mapping)
    if isinstance(comment, InvalidRequest):
        return comment
    return PrepareRequest(
        request_id=request_id,
        session_id=session_id,
        policy_generation=generation,
        source_group_id=source_group_id,
        source_message_id=source_message_id,
        target_group_id=target_group_id,
        comment=comment,
        protocol_version=version,
    )


def parse_stage_execution_request(payload: Any) -> StageExecutionRequest | InvalidRequest:
    """解析源/评论执行请求体：尝试 + 会话 + 权限代次 + 该阶段挑战。"""

    mapping = _as_mapping(payload)
    if isinstance(mapping, InvalidRequest):
        return mapping
    allowed = frozenset(
        {PROTOCOL_VERSION_FIELD, "attempt_id", "session_id", "policy_generation", "challenge"}
    )
    rejected = _reject_unknown(mapping, allowed)
    if rejected is not None:
        return rejected
    version = _parse_version(mapping)
    if isinstance(version, InvalidRequest):
        return version
    attempt_id = _parse_identifier(mapping, "attempt_id")
    if isinstance(attempt_id, InvalidRequest):
        return attempt_id
    session_id = _parse_identifier(mapping, "session_id")
    if isinstance(session_id, InvalidRequest):
        return session_id
    generation = _parse_generation(mapping, "policy_generation")
    if isinstance(generation, InvalidRequest):
        return generation
    challenge = _parse_identifier(mapping, "challenge")
    if isinstance(challenge, InvalidRequest):
        return challenge
    return StageExecutionRequest(
        attempt_id=attempt_id,
        session_id=session_id,
        policy_generation=generation,
        challenge=challenge,
        protocol_version=version,
    )


def parse_cancel_request(payload: Any) -> CancelRequest | InvalidRequest:
    """解析撤销请求体：可选尝试 ID。"""

    mapping = _as_mapping(payload)
    if isinstance(mapping, InvalidRequest):
        return mapping
    allowed = frozenset({PROTOCOL_VERSION_FIELD, "attempt_id"})
    rejected = _reject_unknown(mapping, allowed)
    if rejected is not None:
        return rejected
    version = _parse_version(mapping)
    if isinstance(version, InvalidRequest):
        return version
    attempt_id = ""
    if "attempt_id" in mapping:
        parsed = _parse_identifier(mapping, "attempt_id")
        if isinstance(parsed, InvalidRequest):
            return parsed
        attempt_id = parsed
    return CancelRequest(attempt_id=attempt_id, protocol_version=version)
