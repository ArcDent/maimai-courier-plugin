"""专用转发服务客户端：HTTP 传输、v1 协议调用、账本绑定与有界结果恢复（design D2/D4/D5/D7）。

本模块是插件侧唯一的服务访问面：

- `HttpTransport` 是可注入的传输接缝；真实实现 `AiohttpTransport` **延迟导入** aiohttp
  （未安装时如实报告不可用，不在导入期加载网络库）、固定地址、显式超时、**不跟随
  重定向**（认证头不转交给别的地址）、默认校验 TLS 证书；
- `RemoteServiceClient` 只做「认证头 + 请求体 → 端点 → 关键字段校验」的 v1 协议调用，
  涵盖会话、心跳、策略、准备、源/评论执行、查询、撤销与注销八个客户端动作；
- `RemoteServiceSession` 在上层持有会话、单调权限代次与**账本绑定**：客户端本地与
  服务端上报的账本身份必须与配置的预期值（以及本地迁移收据）一致，否则一律
  `RemoteUnavailable`，绝不回退旧发送通道；
- 所有请求都有上界（`DEFAULT_REQUEST_TIMEOUT_SECONDS`，代码常量）；执行响应丢失时
  **只查询同一投递**、不再提交发送探测（查询与恢复编排见 `forward_delivery.py`）；
- 拒绝面只带机器可读错误类别与可定位中文原因，不回显凭证、正文或响应原文。

`ctx.paths.data_dir` 只保存本模块写下的**非敏感迁移收据**（账本身份 + 迁移确认标识），
不保存凭证，也不保存第二份投递真相（服务端账本是新投递的唯一权威，design D7）。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from .shared.config_rules import is_loopback_host, valid_env_var_name
from .shared.identifiers import safe_identifier
from .shared.protocol import PROTOCOL_VERSION

DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
"""单次服务请求的等待上限（秒，代码常量）：超时按不可用报告，不做成用户配置。"""

MAX_ENDPOINT_LENGTH: Final[int] = 2048
"""服务地址长度上限（防御畸形配置，不参与业务）。"""

SERVICE_BINDING_RECEIPT_FILE_NAME: Final[str] = "service_binding.json"
"""客户端迁移收据文件名（落在 `ctx.paths.data_dir`，只含非敏感账本绑定标识）。"""

ERROR_LEDGER_UNAVAILABLE: Final[str] = "ledger_unavailable"
"""服务端账本不可用的错误类别（`service/coordinator.py` 的公开类别同值）。

客户端据此把「账本不可用，已关闭发送」与普通拒绝区分开：它同样表示本次未发送任何
消息，但需要单独的工具状态（`ledger_unavailable`）。
"""


class TransportUnavailable(Exception):
    """传输层不可用（连接失败、超时、响应不可解析、被重定向）：不含凭证与正文。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HttpTransport(Protocol):
    """HTTP 传输接缝：真实实现是 `AiohttpTransport`，测试注入内存或脚本化替身。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> "TransportReply": ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TransportReply:
    """一次传输的原始回应：状态码 + JSON 对象载荷（非 JSON 由传输层按不可用抛出）。"""

    status: int
    payload: Mapping[str, Any] = field(default_factory=dict)


class AiohttpTransport:
    """真实 aiohttp 传输：固定地址、显式超时、不跟随重定向、默认校验证书。

    延迟导入 aiohttp：宿主未装依赖时导入本模块仍安全，调用时如实报告不可用。
    只向配置的固定地址发请求；`allow_redirects=False` 让 3xx 原样返回，客户端据此
    拒绝，认证头不会随重定向转交（design D5）。
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._session: Any = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportReply:
        try:
            import aiohttp
        except ImportError:
            raise TransportUnavailable("缺少 aiohttp 依赖，无法连接转发服务")
        session = await self._session_or_create(aiohttp)
        try:
            async with session.request(
                method,
                url,
                data=body if body else None,
                headers=dict(headers),
                allow_redirects=False,
            ) as response:
                raw = await response.read()
                status = int(response.status)
        except (asyncio.TimeoutError, TimeoutError):
            raise TransportUnavailable("等待转发服务响应超时") from None
        except aiohttp.ClientError as error:
            raise TransportUnavailable(f"无法连接转发服务（{type(error).__name__}）") from error
        payload = _decode_object(raw)
        if payload is None:
            raise TransportUnavailable("转发服务响应不是合法的 JSON 对象")
        return TransportReply(status=status, payload=payload)

    async def _session_or_create(self, aiohttp_module: Any) -> Any:
        """复用（或建立）一个 aiohttp 会话；TLS 证书按 aiohttp 默认校验，不关闭。"""

        session = self._session
        if session is not None and not session.closed:
            return session
        self._session = aiohttp_module.ClientSession(
            timeout=aiohttp_module.ClientTimeout(total=self._timeout_seconds),
        )
        return self._session

    async def close(self) -> None:
        """幂等关闭底层会话（未建立时什么都不做）。"""

        session = self._session
        self._session = None
        if session is None or session.closed:
            return
        try:
            await session.close()
        except Exception:  # 关闭失败不改变调用方结果，也不抛出网络细节
            return


def _decode_object(raw: bytes) -> Mapping[str, Any] | None:
    """把响应字节解析成 JSON 对象；其余形态返回 `None`（由调用方按不可用处理）。"""

    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    """已校验的服务地址：scheme + 主机 + 可选路径前缀（无查询串/片段/凭证）。"""

    base_url: str

    def url_for(self, path: str) -> str:
        """拼出该地址下的绝对请求地址（路径以 `/` 开头）。"""

        return f"{self.base_url}{path}"


@dataclass(frozen=True, slots=True)
class InvalidEndpoint:
    """服务地址非法：只带可定位中文原因。"""

    reason: str


def parse_service_endpoint(raw: str) -> ServiceEndpoint | InvalidEndpoint:
    """校验服务地址：非空、http(s)、有主机、无凭证/查询串/片段，跨主机必须 https。

    非回环主机允许 `https`（默认校验证书）或运维提供的受控加密隧道（表现为回环
    地址上的明文 http）。绝不接受「远程明文 http」。
    """

    text = str(raw or "").strip()
    if not text:
        return InvalidEndpoint("未配置转发服务地址")
    if len(text) > MAX_ENDPOINT_LENGTH:
        return InvalidEndpoint("转发服务地址过长")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        return InvalidEndpoint("转发服务地址必须以 http:// 或 https:// 开头")
    if parts.username or parts.password:
        return InvalidEndpoint("转发服务地址不能包含凭证")
    if parts.query or parts.fragment:
        return InvalidEndpoint("转发服务地址不能包含查询串或片段")
    host = parts.hostname or ""
    if not host:
        return InvalidEndpoint("转发服务地址缺少主机名")
    if parts.scheme != "https" and not is_loopback_host(host):
        return InvalidEndpoint("跨主机连接必须使用 https（或运维提供的受控加密隧道）")
    return ServiceEndpoint(base_url=urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")))


@dataclass(frozen=True, slots=True)
class RemoteUnavailable:
    """客户端本地或传输层面的不可用：本次调用未产生任何服务端发送。"""

    reason: str


@dataclass(frozen=True, slots=True)
class RemoteRejected:
    """服务端明确拒绝：机器可读错误类别 + 可定位原因（不含请求正文）。"""

    error: str
    reason: str
    status: int = 0


@dataclass(frozen=True, slots=True)
class QueryMissing:
    """查询的投递在服务端不存在：只读事实，不开启新尝试。"""

    reason: str


# --------------------------------------------------------------------------- 视图


@dataclass(frozen=True, slots=True)
class ServiceSessionView:
    """会话建立结果：绑定账号、账本身份与服务启动代次。"""

    session_id: str
    account_id: str
    ledger_identity: str
    server_generation: str


@dataclass(frozen=True, slots=True)
class SessionAliveView:
    """心跳结果：只续期，不改变权限。"""

    session_id: str
    expires_in_seconds: float


@dataclass(frozen=True, slots=True)
class PolicyView:
    """权限代次更新结果。"""

    session_id: str
    policy_generation: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class PrepareView:
    """准备结果：不发送；含既有阻断状态与源阶段单次挑战。"""

    delivery_id: str
    attempt_id: str
    status: str
    challenge: str = ""
    challenge_expires_in_seconds: float = 0.0
    target_message_id: str = ""
    comment_status: str = "not_requested"


@dataclass(frozen=True, slots=True)
class StageView:
    """源/评论阶段执行结果：权威状态 + 已知目标 ID。"""

    delivery_id: str
    attempt_id: str
    stage: str
    status: str
    target_message_id: str = ""
    comment_status: str = "not_requested"
    comment_message_id: str = ""
    comment_challenge: str = ""
    comment_challenge_expires_in_seconds: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """只读查询结果：权威源/评论状态与已知目标 ID，不含任何正文。"""

    delivery_id: str
    status: str
    target_message_id: str = ""
    comment_status: str = "not_requested"
    comment_message_id: str = ""
    comment_challenge: str = ""
    comment_challenge_expires_in_seconds: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CancelView:
    """撤销结果：已撤销 / 已开始 / 已完成 / 无此投递。"""

    delivery_id: str
    status: str
    reason: str = ""


# --------------------------------------------------------------------------- 客户端


class RemoteServiceClient:
    """v1 协议客户端：只做认证头、请求体、端点与关键字段校验，不保存任何投递状态。"""

    def __init__(
        self,
        *,
        endpoint: ServiceEndpoint,
        client_identity: str,
        token_env_var: str,
        transport: HttpTransport,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._client_identity = client_identity
        self._token_env_var = token_env_var
        self._transport = transport
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._environ = environ

    @property
    def endpoint(self) -> ServiceEndpoint:
        return self._endpoint

    @property
    def token_env_var(self) -> str:
        """凭证环境变量名（只暴露名字，绝不暴露凭证值）。"""

        return self._token_env_var

    def credential_available(self) -> bool:
        """凭证环境变量是否已设置且非空白（只判存在性，不读取值给调用方）。"""

        return self._credential() is not None

    def _credential(self) -> str | None:
        """从指定环境变量取凭证；缺省或全空白返回 `None`。"""

        source = self._environ if self._environ is not None else os.environ
        token = str(source.get(self._token_env_var) or "").strip()
        return token or None

    async def close(self) -> None:
        """关闭传输（幂等；关闭失败不影响调用方结果）。"""

        try:
            await self._transport.close()
        except Exception:
            return

    # ------------------------------------------------------------------ 原始调用

    async def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | RemoteUnavailable | RemoteRejected:
        """发起一次有界请求：认证头 + JSON 请求体 → 解析出的 `result` 对象。

        传输失败、超时与 3xx 一律按 `RemoteUnavailable`（客户端不从重定向取任何
        信息）；非 200 按 `RemoteRejected` 带回服务端的错误类别与原因。
        """

        token = self._credential()
        if token is None:
            return RemoteUnavailable(f"缺少凭证环境变量 {self._token_env_var}")
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            reply = await asyncio.wait_for(
                self._transport.request(
                    method,
                    self._endpoint.url_for(path),
                    headers=headers,
                    body=body,
                ),
                timeout=self._request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return RemoteUnavailable("等待转发服务响应超时")
        except TransportUnavailable as error:
            return RemoteUnavailable(error.reason)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # 传输替身可能抛任意异常：一律按不可用，不回显细节
            return RemoteUnavailable(f"转发服务调用失败（{type(error).__name__}）")
        if 300 <= reply.status < 400:
            return RemoteUnavailable("转发服务返回重定向，客户端不跟随（认证头不转交）")
        if reply.status == 200:
            result = reply.payload.get("result")
            if reply.payload.get("ok") is not True or not isinstance(result, Mapping):
                return RemoteUnavailable("转发服务响应不符合协议")
            return dict(result)
        return RemoteRejected(
            error=str(reply.payload.get("error") or f"http_{reply.status}"),
            reason=str(reply.payload.get("reason") or "转发服务拒绝了本次请求"),
            status=reply.status,
        )

    # ------------------------------------------------------------------ 协议动作

    async def open_session(self) -> ServiceSessionView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/sessions`：建立认证会话并返回绑定账号与账本身份。"""

        result = await self._call(
            "POST",
            "/v1/sessions",
            {"protocol_version": PROTOCOL_VERSION, "client_identity": self._client_identity},
        )
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        session_id = _required_text(result, "session_id")
        account_id = _required_text(result, "account_id")
        ledger_identity = _required_text(result, "ledger_identity")
        server_generation = _required_text(result, "server_generation")
        if isinstance(session_id, RemoteUnavailable):
            return session_id
        if isinstance(account_id, RemoteUnavailable):
            return account_id
        if isinstance(ledger_identity, RemoteUnavailable):
            return ledger_identity
        if isinstance(server_generation, RemoteUnavailable):
            return server_generation
        return ServiceSessionView(
            session_id=session_id,
            account_id=account_id,
            ledger_identity=ledger_identity,
            server_generation=server_generation,
        )

    async def heartbeat(self, session_id: str) -> SessionAliveView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/sessions/{id}/heartbeat`：只续期，不发送、不更新权限。"""

        result = await self._call(
            "POST",
            f"/v1/sessions/{quote(session_id, safe='')}/heartbeat",
            {"protocol_version": PROTOCOL_VERSION},
        )
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        return SessionAliveView(
            session_id=_optional_text(result, "session_id") or session_id,
            expires_in_seconds=_optional_float(result, "expires_in_seconds"),
        )

    async def update_policy(
        self,
        session_id: str,
        *,
        generation: int,
        enabled: bool,
        source_groups: tuple[str, ...],
        target_groups: tuple[str, ...],
    ) -> PolicyView | RemoteUnavailable | RemoteRejected:
        """`PUT /v1/sessions/{id}/policy`：单调递增的权限代次与许可集合。"""

        result = await self._call(
            "PUT",
            f"/v1/sessions/{quote(session_id, safe='')}/policy",
            {
                "protocol_version": PROTOCOL_VERSION,
                "policy_generation": int(generation),
                "enabled": bool(enabled),
                "source_groups": list(source_groups),
                "target_groups": list(target_groups),
            },
        )
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        policy_generation = _required_int(result, "policy_generation")
        if isinstance(policy_generation, RemoteUnavailable):
            return policy_generation
        return PolicyView(
            session_id=_optional_text(result, "session_id") or session_id,
            policy_generation=policy_generation,
            enabled=bool(result.get("enabled")),
        )

    async def deregister(self, session_id: str) -> None | RemoteUnavailable | RemoteRejected:
        """`DELETE /v1/sessions/{id}`：注销会话并撤销其未消费阶段授权。"""

        result = await self._call("DELETE", f"/v1/sessions/{quote(session_id, safe='')}")
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        return None

    async def prepare(
        self,
        *,
        request_id: str,
        session_id: str,
        policy_generation: int,
        source_group_id: str,
        source_message_id: str,
        target_group_id: str,
        comment: str = "",
    ) -> PrepareView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/forwards/prepare`：只校验与登记，不发送。"""

        result = await self._call(
            "POST",
            "/v1/forwards/prepare",
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "session_id": session_id,
                "policy_generation": int(policy_generation),
                "source_group_id": source_group_id,
                "source_message_id": source_message_id,
                "target_group_id": target_group_id,
                "comment": comment,
            },
        )
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        delivery_id = _required_text(result, "delivery_id")
        status = _required_text(result, "status")
        if isinstance(delivery_id, RemoteUnavailable):
            return delivery_id
        if isinstance(status, RemoteUnavailable):
            return status
        return PrepareView(
            delivery_id=delivery_id,
            attempt_id=_optional_text(result, "attempt_id"),
            status=status,
            challenge=_optional_text(result, "challenge"),
            challenge_expires_in_seconds=_optional_float(result, "challenge_expires_in_seconds"),
            target_message_id=_optional_text(result, "target_message_id"),
            comment_status=_optional_text(result, "comment_status") or "not_requested",
        )

    async def execute_stage(
        self,
        *,
        delivery_id: str,
        stage: str,
        attempt_id: str,
        session_id: str,
        policy_generation: int,
        challenge: str,
    ) -> StageView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/forwards/{id}/{stage}`：提交该阶段的单次授权。"""

        result = await self._call(
            "POST",
            f"/v1/forwards/{quote(delivery_id, safe='')}/{quote(stage, safe='')}",
            {
                "protocol_version": PROTOCOL_VERSION,
                "attempt_id": attempt_id,
                "session_id": session_id,
                "policy_generation": int(policy_generation),
                "challenge": challenge,
            },
        )
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        status = _required_text(result, "status")
        if isinstance(status, RemoteUnavailable):
            return status
        return StageView(
            delivery_id=_optional_text(result, "delivery_id") or delivery_id,
            attempt_id=_optional_text(result, "attempt_id") or attempt_id,
            stage=_optional_text(result, "stage") or stage,
            status=status,
            target_message_id=_optional_text(result, "target_message_id"),
            comment_status=_optional_text(result, "comment_status") or "not_requested",
            comment_message_id=_optional_text(result, "comment_message_id"),
            comment_challenge=_optional_text(result, "comment_challenge"),
            comment_challenge_expires_in_seconds=_optional_float(
                result, "comment_challenge_expires_in_seconds"
            ),
            reason=_optional_text(result, "reason"),
        )

    async def execute_source(
        self,
        *,
        delivery_id: str,
        attempt_id: str,
        session_id: str,
        policy_generation: int,
        challenge: str,
    ) -> StageView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/forwards/{id}/source`（源阶段）。"""

        return await self.execute_stage(
            delivery_id=delivery_id,
            stage="source",
            attempt_id=attempt_id,
            session_id=session_id,
            policy_generation=policy_generation,
            challenge=challenge,
        )

    async def execute_comment(
        self,
        *,
        delivery_id: str,
        attempt_id: str,
        session_id: str,
        policy_generation: int,
        challenge: str,
    ) -> StageView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/forwards/{id}/comment`（评论阶段）。"""

        return await self.execute_stage(
            delivery_id=delivery_id,
            stage="comment",
            attempt_id=attempt_id,
            session_id=session_id,
            policy_generation=policy_generation,
            challenge=challenge,
        )

    async def query(
        self, delivery_id: str
    ) -> DeliveryView | QueryMissing | RemoteUnavailable | RemoteRejected:
        """`GET /v1/forwards/{id}`：只读已持久化结果，不触发任何发送。"""

        result = await self._call("GET", f"/v1/forwards/{quote(delivery_id, safe='')}")
        if isinstance(result, RemoteRejected):
            if result.status == 404:
                return QueryMissing(reason=result.reason)
            return result
        if isinstance(result, RemoteUnavailable):
            return result
        status = _required_text(result, "status")
        if isinstance(status, RemoteUnavailable):
            return status
        return DeliveryView(
            delivery_id=_optional_text(result, "delivery_id") or delivery_id,
            status=status,
            target_message_id=_optional_text(result, "target_message_id"),
            comment_status=_optional_text(result, "comment_status") or "not_requested",
            comment_message_id=_optional_text(result, "comment_message_id"),
            comment_challenge=_optional_text(result, "comment_challenge"),
            comment_challenge_expires_in_seconds=_optional_float(
                result, "comment_challenge_expires_in_seconds"
            ),
            reason=_optional_text(result, "reason"),
        )

    async def cancel(
        self, *, delivery_id: str, attempt_id: str = ""
    ) -> CancelView | RemoteUnavailable | RemoteRejected:
        """`POST /v1/forwards/{id}/cancel`：撤销未消费授权或报告在途/已完成。"""

        payload: dict[str, Any] = {"protocol_version": PROTOCOL_VERSION}
        if attempt_id:
            payload["attempt_id"] = attempt_id
        result = await self._call(
            "POST", f"/v1/forwards/{quote(delivery_id, safe='')}/cancel", payload
        )
        if isinstance(result, (RemoteUnavailable, RemoteRejected)):
            return result
        return CancelView(
            delivery_id=_optional_text(result, "delivery_id") or delivery_id,
            status=_optional_text(result, "status"),
            reason=_optional_text(result, "reason"),
        )


def _required_text(payload: Mapping[str, Any], name: str) -> str | RemoteUnavailable:
    """必填文本字段：缺失或空串按协议不符报告（不猜测、不编造默认值）。"""

    value = payload.get(name)
    if not isinstance(value, str) or not value:
        return RemoteUnavailable(f"转发服务响应缺少字段 {name}")
    return value


def _optional_text(payload: Mapping[str, Any], name: str) -> str:
    """可选文本字段：非字符串按缺省处理（只影响定位信息，不影响安全判定）。"""

    value = payload.get(name)
    return value if isinstance(value, str) else ""


def _optional_float(payload: Mapping[str, Any], name: str) -> float:
    """可选数值字段：非数值按 0 处理（它只用于展示与定时，不参与安全判定）。"""

    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _required_int(payload: Mapping[str, Any], name: str) -> int | RemoteUnavailable:
    """必填整数字段：布尔与缺失都按协议不符报告。"""

    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return RemoteUnavailable(f"转发服务响应缺少字段 {name}")
    return value


# --------------------------------------------------------------------------- 账本绑定


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    """客户端账本绑定：预期账本身份 + 迁移确认标识（都不含凭证）。"""

    expected_ledger_id: str
    migration_confirmation: str


@dataclass(frozen=True, slots=True)
class ServiceReceipt:
    """本地迁移收据：曾经成功绑定的账本身份与迁移确认标识。"""

    ledger_identity: str
    migration_confirmation: str


def read_service_receipt(path: Path) -> ServiceReceipt | None | RemoteUnavailable:
    """读取本地迁移收据：不存在返回 `None`；存在但损坏/非法返回不可用（不当成不存在）。"""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        return RemoteUnavailable(f"无法读取本地迁移收据（{type(error).__name__}）")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return RemoteUnavailable("本地迁移收据不是合法 JSON（不当作未绑定）")
    if not isinstance(decoded, Mapping):
        return RemoteUnavailable("本地迁移收据结构不符（不当作未绑定）")
    ledger_identity = decoded.get("ledger_identity")
    migration_confirmation = decoded.get("migration_confirmation")
    if not isinstance(ledger_identity, str) or not isinstance(migration_confirmation, str):
        return RemoteUnavailable("本地迁移收据缺少账本身份或迁移确认标识（不当作未绑定）")
    return ServiceReceipt(
        ledger_identity=ledger_identity,
        migration_confirmation=migration_confirmation,
    )


def write_service_receipt(path: Path, receipt: ServiceReceipt) -> RemoteUnavailable | None:
    """写入非敏感迁移收据（原子替换）；失败返回不可用，绝不写凭证。"""

    payload = {
        "ledger_identity": receipt.ledger_identity,
        "migration_confirmation": receipt.migration_confirmation,
    }
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return RemoteUnavailable(f"无法写入本地迁移收据（{type(error).__name__}）")
    return None


def check_service_binding(
    *,
    endpoint: str,
    client_identity: str,
    token_env_var: str,
    binding: ServiceBinding,
    receipt_path: Path,
    environ: Mapping[str, str] | None = None,
) -> RemoteUnavailable | None:
    """本地（不联网）绑定检查：地址/身份/变量名/凭证/预期账本/收据逐项核对。

    任一项不满足都返回 `RemoteUnavailable`：缺少配置或凭证、预期账本 ID 为空、
    迁移确认标识为空、收据与当前配置不一致（疑似换库）都在发送前被拒绝，且不回退
    旧发送通道。收据存在但损坏时不当作「未绑定」。
    """

    parsed = parse_service_endpoint(endpoint)
    if isinstance(parsed, InvalidEndpoint):
        return RemoteUnavailable(parsed.reason)
    identity = safe_identifier(client_identity)
    if not identity:
        return RemoteUnavailable("客户端身份名不是安全标识符（非空、无空白/控制字符/尖括号/引号/反引号）")
    env_var = str(token_env_var or "").strip()
    if not valid_env_var_name(env_var):
        return RemoteUnavailable("凭证环境变量名非法（只允许字母、数字与下划线，且不以数字开头）")
    expected = str(binding.expected_ledger_id or "").strip()
    if not expected:
        return RemoteUnavailable("未配置预期账本 ID：无法确认转发服务数据集，已关闭发送")
    if not safe_identifier(expected):
        return RemoteUnavailable("预期账本 ID 不是安全标识符")
    confirmation = str(binding.migration_confirmation or "").strip()
    if not confirmation:
        return RemoteUnavailable("未填写迁移确认标识：未显式确认新装/迁移数据集，已关闭发送")
    source = environ if environ is not None else os.environ
    if not str(source.get(env_var) or "").strip():
        return RemoteUnavailable(f"缺少凭证环境变量 {env_var}")
    receipt = read_service_receipt(receipt_path)
    if isinstance(receipt, RemoteUnavailable):
        return receipt
    if receipt is not None and (
        receipt.ledger_identity != expected or receipt.migration_confirmation != confirmation
    ):
        return RemoteUnavailable("本地迁移收据与当前配置不一致（疑似连到了另一数据集），已关闭发送")
    return None


# --------------------------------------------------------------------------- 会话


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    """客户端发送许可快照：总开关 + 两级白名单（服务端再与自身上限取交集）。"""

    enabled: bool
    source_groups: tuple[str, ...]
    target_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """`[service]` 配置节的只读字段面（构造 `RemoteServiceSession` 用）。"""

    endpoint: str
    client_identity: str
    token_env_var: str
    expected_ledger_id: str
    migration_confirmation: str

    @classmethod
    def from_config(cls, section: Any) -> "ServiceSettings":
        """从配置节（或等价映射）读出字段面；缺字段回落空串，不猜测。"""

        def text(name: str) -> str:
            value = getattr(section, name, None)
            if value is None and isinstance(section, Mapping):
                value = section.get(name)
            return str(value or "").strip()

        return cls(
            endpoint=text("endpoint"),
            client_identity=text("client_identity"),
            token_env_var=text("token_env_var"),
            expected_ledger_id=text("expected_ledger_id"),
            migration_confirmation=text("migration_confirmation"),
        )

    def binding(self) -> ServiceBinding:
        """取账本绑定（预期身份 + 迁移确认标识）。"""

        return ServiceBinding(
            expected_ledger_id=self.expected_ledger_id,
            migration_confirmation=self.migration_confirmation,
        )

    def signature(self) -> tuple[str, ...]:
        """配置签名：变化时插件重建客户端会话（不复用旧地址/旧身份）。"""

        return (
            self.endpoint,
            self.client_identity,
            self.token_env_var,
            self.expected_ledger_id,
            self.migration_confirmation,
        )


class RemoteServiceSession:
    """插件持有的服务会话：会话/代次/账本绑定 + 会话级协议动作。

    - 静态检查（`static_check`）不联网，可在插件加载时先行核对；
    - `ensure_ready` 建立（或续期）会话、核对服务端上报的账本身份、写入迁移收据，
      并在许可快照变化时推进策略代次；
    - `advance_policy` 只在**发送许可**变化时推进代次（仅标准/候选类型变化不属于
      权限代次更新）；会话不可用时不伪称已通知；
    - 所有动作都返回明确结果类型，绝不抛出网络细节或凭证。
    """

    def __init__(
        self,
        *,
        settings: ServiceSettings,
        receipt_path: Path,
        transport: HttpTransport,
        environ: Mapping[str, str] | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._receipt_path = receipt_path
        endpoint = parse_service_endpoint(settings.endpoint)
        if isinstance(endpoint, InvalidEndpoint):
            # 静态检查会先拦下非法地址；这里用不可用地址构造占位客户端不成立，
            # 因此把错误记为会话级不可用，任何动作都返回同一原因。
            self._client: RemoteServiceClient | None = None
            self._construction_failure = RemoteUnavailable(endpoint.reason)
        else:
            self._client = RemoteServiceClient(
                endpoint=endpoint,
                client_identity=settings.client_identity,
                token_env_var=settings.token_env_var,
                transport=transport,
                request_timeout_seconds=request_timeout_seconds,
                environ=environ,
            )
            self._construction_failure = None
        self._environ = environ
        self._session_id = ""
        self._policy_generation = 0
        self._ledger_identity = ""
        self._pushed_policy: ServicePolicy | None = None

    @property
    def session_id(self) -> str:
        """当前会话 ID（空串表示尚未建立）。"""

        return self._session_id

    @property
    def policy_generation(self) -> int:
        """当前已下发的权限代次（0 表示尚未下发）。"""

        return self._policy_generation

    @property
    def ledger_identity(self) -> str:
        """服务端上报并已核对的账本身份（空串表示尚未核对）。"""

        return self._ledger_identity

    def static_check(self) -> RemoteUnavailable | None:
        """不联网的绑定检查（地址、身份、变量名、凭证、预期账本、收据）。"""

        if self._construction_failure is not None or self._client is None:
            return self._construction_failure or RemoteUnavailable("转发服务客户端不可用")
        return check_service_binding(
            endpoint=self._settings.endpoint,
            client_identity=self._settings.client_identity,
            token_env_var=self._settings.token_env_var,
            binding=self._settings.binding(),
            receipt_path=self._receipt_path,
            environ=self._environ,
        )

    async def close(self) -> None:
        """关闭传输并清空会话状态（幂等）。"""

        self._session_id = ""
        self._policy_generation = 0
        self._pushed_policy = None
        if self._client is not None:
            await self._client.close()

    # ------------------------------------------------------------------ 会话就绪

    async def ensure_ready(self, policy: ServicePolicy) -> RemoteUnavailable | None:
        """确保会话可用、账本绑定已核对、许可与最新快照一致；失败返回原因。"""

        unavailable = self.static_check()
        if unavailable is not None:
            return unavailable
        assert self._client is not None
        if self._session_id:
            alive = await self._client.heartbeat(self._session_id)
            if isinstance(alive, RemoteUnavailable):
                return alive
            if isinstance(alive, RemoteRejected):
                # 会话失效（被新实例替换、过期或服务重启）：丢弃旧会话，重建一次。
                self._session_id = ""
                self._policy_generation = 0
                self._pushed_policy = None
        if not self._session_id:
            opened = await self._client.open_session()
            if isinstance(opened, RemoteUnavailable):
                return opened
            if isinstance(opened, RemoteRejected):
                return RemoteUnavailable(
                    f"转发服务拒绝建立会话（{opened.error}：{opened.reason}）"
                )
            if opened.ledger_identity != self._settings.expected_ledger_id:
                return RemoteUnavailable(
                    "转发服务账本身份与预期不一致（疑似连到了另一数据集），已关闭发送"
                )
            written = write_service_receipt(
                self._receipt_path,
                ServiceReceipt(
                    ledger_identity=opened.ledger_identity,
                    migration_confirmation=self._settings.migration_confirmation,
                ),
            )
            if written is not None:
                return written
            self._session_id = opened.session_id
            self._policy_generation = 0
            self._ledger_identity = opened.ledger_identity
            self._pushed_policy = None
        if self._pushed_policy is None or self._pushed_policy != policy:
            return await self._push_policy(policy)
        return None

    async def advance_policy(self, policy: ServicePolicy) -> RemoteUnavailable | None:
        """许可变化后推进远端代次；会话未建立或快照未变时不通知（返回 `None`）。

        返回 `RemoteUnavailable` 表示**撤销未确认**：调用方不得宣称远端已停止。
        """

        if self._client is None or not self._session_id:
            return None
        if self._pushed_policy == policy:
            return None
        return await self._push_policy(policy)

    async def _push_policy(self, policy: ServicePolicy) -> RemoteUnavailable | None:
        """下发下一版权限快照（代次严格递增；服务端仍与自身上限取交集）。"""

        assert self._client is not None
        generation = self._policy_generation + 1
        accepted = await self._client.update_policy(
            self._session_id,
            generation=generation,
            enabled=policy.enabled,
            source_groups=policy.source_groups,
            target_groups=policy.target_groups,
        )
        if isinstance(accepted, RemoteUnavailable):
            return accepted
        if isinstance(accepted, RemoteRejected):
            return RemoteUnavailable(f"转发服务拒绝权限更新（{accepted.error}：{accepted.reason}）")
        self._policy_generation = generation
        self._pushed_policy = policy
        return None

    async def deregister(self) -> RemoteUnavailable | None:
        """注销会话（撤销未消费阶段授权）；返回 `None` 表示远端已确认。"""

        if self._client is None or not self._session_id:
            return None
        session_id = self._session_id
        self._session_id = ""
        self._policy_generation = 0
        self._pushed_policy = None
        outcome = await self._client.deregister(session_id)
        if isinstance(outcome, RemoteUnavailable):
            return outcome
        return None

    # ------------------------------------------------------------------ 阶段动作

    async def prepare(
        self,
        *,
        request_id: str,
        source_group_id: str,
        source_message_id: str,
        target_group_id: str,
        comment: str = "",
    ) -> PrepareView | RemoteUnavailable | RemoteRejected:
        """远端准备（只校验、查源与登记，不发送）。"""

        if self._client is None or not self._session_id:
            return RemoteUnavailable("尚未建立转发服务会话")
        return await self._client.prepare(
            request_id=request_id,
            session_id=self._session_id,
            policy_generation=self._policy_generation,
            source_group_id=source_group_id,
            source_message_id=source_message_id,
            target_group_id=target_group_id,
            comment=comment,
        )

    async def execute_source(
        self, *, delivery_id: str, attempt_id: str, challenge: str
    ) -> StageView | RemoteUnavailable | RemoteRejected:
        """提交源阶段单次授权。"""

        if self._client is None or not self._session_id:
            return RemoteUnavailable("尚未建立转发服务会话")
        return await self._client.execute_source(
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            session_id=self._session_id,
            policy_generation=self._policy_generation,
            challenge=challenge,
        )

    async def execute_comment(
        self, *, delivery_id: str, attempt_id: str, challenge: str
    ) -> StageView | RemoteUnavailable | RemoteRejected:
        """提交评论阶段单次授权。"""

        if self._client is None or not self._session_id:
            return RemoteUnavailable("尚未建立转发服务会话")
        return await self._client.execute_comment(
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            session_id=self._session_id,
            policy_generation=self._policy_generation,
            challenge=challenge,
        )

    async def query(
        self, delivery_id: str
    ) -> DeliveryView | QueryMissing | RemoteUnavailable | RemoteRejected:
        """只读查询同一投递的权威状态（响应丢失后的唯一恢复动作）。"""

        if self._client is None or not self._session_id:
            return RemoteUnavailable("尚未建立转发服务会话")
        return await self._client.query(delivery_id)

    async def cancel(
        self, *, delivery_id: str, attempt_id: str = ""
    ) -> CancelView | RemoteUnavailable | RemoteRejected:
        """请求远端撤销未消费授权（在途只报告，不承诺撤回）。"""

        if self._client is None or not self._session_id:
            return RemoteUnavailable("尚未建立转发服务会话")
        return await self._client.cancel(delivery_id=delivery_id, attempt_id=attempt_id)
