"""SnowLuma 固定动作 HTTP 传输与纯配置解析；网络库延迟到发出请求前导入。

本模块只做传输与端点安全判定，不感知消息类型语义（分派归 `snowluma_client`）。
契约要点：无连接，按请求 `POST /{action}`；凭证仅经 `Authorization: Bearer` 头；
禁跟随重定向；请求体自设 2MiB 上限，序列化后超限在发出前拒绝；明文建连前解析
主机名判定可信网络；单实例串行执行且不重放动作。固定七动作加关闭，没有任意动作
代理：只读 `get_login_info` / `get_group_list` / `get_msg` / `get_forward_msg`，
发送 `send_msg` / `send_forward_msg` / `send_group_forward_msg`（卡片路径的实测
契约见 `docs/snowluma-http-api-catalog.md`）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .config import CourierConfig

from .shared.clock import Clock, SystemClock
from .shared.config_rules import is_loopback_host, is_private_host, onebot_endpoint_rejection, valid_env_var_name

ONEBOT_ACTION_TIMEOUT_SECONDS: Final[float] = 10.0
"""单个动作排队、写入和响应共用的绝对期限。"""

ONEBOT_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
"""单独关闭的上限；上层可提前取消以共享更小的卸载剩余预算。"""

ONEBOT_MAX_REQUEST_BYTES: Final[int] = 2 * 1024 * 1024
"""客户端自设请求体上限：低于实测服务端上限（约 2–3MB）留余量。"""

_DEFAULT_HTTP_PORT: Final[int] = 80
"""地址未显式带端口时解析用的默认 HTTP 端口（只影响解析注入点，不改写端点）。"""

HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]
"""发出请求前的主机名解析注入点：返回全部解析地址文本，解析失败抛异常。"""


async def _resolve_host_addresses(host: str, port: int) -> Sequence[str]:
    """用运行循环解析主机名，返回全部地址文本；不做 DNS 缓存、不改变端点。"""

    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(record[4][0]) for record in records]


@dataclass(frozen=True, slots=True)
class OneBotConnection:
    """固定连接描述；配置令牌不参与 repr，避免诊断回显。"""

    endpoint: str
    token_env_var: str = ""
    token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class OneBotUnresolved:
    """安全诊断；区分非法配置和配置尚未完成。"""

    reason: str
    config_failure: bool = False


def resolve_onebot_connection(
    config: CourierConfig | Mapping[str, Any], environ: Mapping[str, str]
) -> OneBotConnection | OneBotUnresolved:
    """只解析受控配置，不发出请求、不读取环境变量中的凭证值。"""

    if isinstance(config, Mapping):
        from .config import CourierConfig

        try:
            config = CourierConfig(**dict(config))
        except ValueError as error:
            return OneBotUnresolved(str(error), config_failure=True)
    endpoint = config.snowluma.http_url
    if not endpoint:
        return OneBotUnresolved("未配置 SnowLuma HTTP 地址")
    reason = onebot_endpoint_rejection(endpoint)
    if reason:
        return OneBotUnresolved(reason, config_failure=bool(endpoint))
    token = config.snowluma.token
    if not isinstance(token, str):
        return OneBotUnresolved("连接令牌必须为字符串", config_failure=True)
    if token.strip():
        return OneBotConnection(endpoint, config.snowluma.token_env_var, token)
    token_env_var = config.snowluma.token_env_var
    if isinstance(token_env_var, str) and not token_env_var:
        return OneBotUnresolved("配置了直连地址就必须填写令牌或凭证环境变量名", config_failure=True)
    if not isinstance(token_env_var, str) or not valid_env_var_name(token_env_var):
        return OneBotUnresolved(
            "凭证环境变量名非法（只允许字母、数字与下划线，且不以数字开头）", config_failure=True,
        )
    # Mapping 的默认 __contains__ 会调用 __getitem__；只遍历名称才能保证零取值。
    if not any(name == token_env_var for name in environ):
        return OneBotUnresolved("指定的凭证环境变量未设置", config_failure=True)
    return OneBotConnection(endpoint, token_env_var)


UnavailableCategory = Literal[
    "configuration", "closed", "insecure_endpoint", "credentials", "authentication",
    "permission", "request_too_large", "timeout", "cancelled", "transport_unavailable",
    "unexpected_response", "close_incomplete", "close_timeout", "close_pending",
]
"""传输不可用的安全类别全集（客户端与测试只依赖这些字面量，拼写漂移由类型检查兜住）。"""


class OneBotUnavailable(RuntimeError):
    """只暴露安全类别；submitted 为可能进入写入，默认保守为真。"""

    def __init__(self, category: UnavailableCategory = "transport_unavailable", *, submitted: bool = True):
        self.category = category
        self.submitted = submitted
        super().__init__(f"OneBot 传输不可用（{category}）")


def _consume_exception(task: asyncio.Future[Any]) -> None:
    """收走本实例后台工作异常，不回显网络库携带的敏感载荷。"""
    if not task.cancelled():
        task.exception()


_ENVELOPE_STATUSES: Final[frozenset[str]] = frozenset({"ok", "failed", "async"})
_EVENT_KEYS: Final[tuple[str, ...]] = (
    "post_type", "message_type", "meta_event_type", "notice_type", "request_type",
)


def _is_action_envelope(payload: object) -> bool:
    """只认动作应答信封；事件、错形态与契约外状态不算响应。"""

    return (isinstance(payload, Mapping)
            and not any(key in payload for key in _EVENT_KEYS)
            and isinstance(payload.get("status"), str)
            and payload["status"] in _ENVELOPE_STATUSES
            and type(payload.get("retcode")) is int
            and "data" in payload
            and isinstance(payload["data"], (dict, list, type(None))))


@runtime_checkable
class OneBotTransport(Protocol):
    """固定动作访问面：七个动作加关闭；不提供任意动作代理，不建立长连接。

    只读四个：`get_login_info` / `get_group_list` / `get_msg` / `get_forward_msg`
    （后者按源卡片标识展开查询合并转发节点）；发送三个：`send_msg`（普通图片段与
    评论）、`send_forward_msg`（普通文字单节点卡片）、`send_group_forward_msg`
    （卡片展开节点重组投递）。
    """

    async def get_login_info(self) -> Mapping[str, Any]: ...
    async def get_group_list(self) -> Mapping[str, Any]: ...
    async def get_msg(self, message_id: str) -> Mapping[str, Any]: ...
    async def get_forward_msg(self, id: str) -> Mapping[str, Any]: ...
    async def send_msg(
        self, *, target_group_id: str, message: Sequence[Mapping[str, Any]],
        before_submit: Callable[[], bool],
    ) -> Mapping[str, Any]: ...
    async def send_forward_msg(
        self, *, target_group_id: str, messages: Sequence[Mapping[str, Any]],
        before_submit: Callable[[], bool],
    ) -> Mapping[str, Any]: ...
    async def send_group_forward_msg(
        self, *, target_group_id: str, messages: Sequence[Mapping[str, Any]],
        before_submit: Callable[[], bool],
    ) -> Mapping[str, Any]: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class _Submission:
    """本次调用的提交证据；取消后禁止尚未开始的写入。"""

    submitted: bool = False
    stopped: bool = False


class AiohttpOneBotTransport:
    """无连接 HTTP 传输：构造不联网，单实例串行执行且不重放动作。"""

    def __init__(
        self, connection: OneBotConnection, *, clock: Clock | None = None,
        environ: Mapping[str, str] | None = None,
        session_factory: Callable[..., Any] | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock if clock is not None else SystemClock()
        self._environ = environ if environ is not None else os.environ
        self._session_factory = session_factory
        self._resolver = resolver if resolver is not None else _resolve_host_addresses
        self._lock = asyncio.Lock()
        self._session: Any = None
        self._closed = False
        self._close_deadline: float | None = None
        self._operations: set[asyncio.Task[Any]] = set()

    async def get_login_info(self) -> Mapping[str, Any]:
        return await self._call("get_login_info", {})

    async def get_group_list(self) -> Mapping[str, Any]:
        return await self._call("get_group_list", {})

    async def get_msg(self, message_id: str) -> Mapping[str, Any]:
        return await self._call("get_msg", {"message_id": message_id})

    async def get_forward_msg(self, id: str) -> Mapping[str, Any]:
        """按源卡片标识展开查询合并转发节点；只读，不设提交许可闸门。"""

        return await self._call("get_forward_msg", {"id": id})

    async def send_msg(
        self, *, target_group_id: str, message: Sequence[Mapping[str, Any]],
        before_submit: Callable[[], bool],
    ) -> Mapping[str, Any]:
        return await self._call("send_msg", {
            "message_type": "group", "group_id": target_group_id,
            "message": [dict(segment) for segment in message],
        }, before_submit)

    async def send_forward_msg(
        self, *, target_group_id: str, messages: Sequence[Mapping[str, Any]],
        before_submit: Callable[[], bool],
    ) -> Mapping[str, Any]:
        # 自定义节点卡片；不设置 summary/news/prompt/source 等预览类参数。
        return await self._call("send_forward_msg",
                                {"group_id": target_group_id, "messages": [dict(node) for node in messages]},
                                before_submit)

    async def send_group_forward_msg(
        self, *, target_group_id: str, messages: Sequence[Mapping[str, Any]],
        before_submit: Callable[[], bool],
    ) -> Mapping[str, Any]:
        # 卡片展开节点重组投递：节点数组只来自 get_forward_msg 展开查询结果；
        # 不设置预览类参数，外层预览由平台默认生成。
        return await self._call("send_group_forward_msg",
                                {"group_id": target_group_id, "messages": [dict(node) for node in messages]},
                                before_submit)

    async def close(self) -> None:
        """幂等且有界：重复调用立即返回；取消在途请求并在剩余预算内关闭会话。"""

        if self._closed and self._session is None:
            return
        self._closed = True
        for operation in tuple(self._operations):
            operation.cancel()
        session, self._session = self._session, None
        if session is None:
            return
        if self._close_deadline is None:
            self._close_deadline = self._clock.now() + ONEBOT_CLOSE_TIMEOUT_SECONDS
        closer = asyncio.ensure_future(session.close())
        try:
            await self._race_until(closer, self._close_deadline,
                                   lambda: OneBotUnavailable("close_incomplete", submitted=False))
        except (Exception, asyncio.CancelledError):
            raise OneBotUnavailable("close_incomplete", submitted=False) from None

    async def _sleep_until(self, deadline: float) -> None:
        await self._clock.sleep(max(0.0, deadline - self._clock.now()))

    async def _call(
        self, action: str, params: Mapping[str, Any], before_submit: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        if self._closed:
            raise OneBotUnavailable("closed", submitted=False)
        deadline = self._clock.now() + ONEBOT_ACTION_TIMEOUT_SECONDS
        state = _Submission()
        operation = asyncio.create_task(self._execute(action, params, before_submit, deadline, state))
        self._operations.add(operation)
        operation.add_done_callback(self._operations.discard)
        operation.add_done_callback(_consume_exception)
        try:
            return await self._race_until(operation, deadline,
                                          lambda: OneBotUnavailable("timeout", submitted=state.submitted))
        except asyncio.CancelledError:
            raise OneBotUnavailable("cancelled", submitted=state.submitted) from None
        except OneBotUnavailable:
            raise
        except Exception:
            raise OneBotUnavailable("transport_unavailable", submitted=state.submitted) from None
        finally:
            state.stopped = True

    async def _race_until(self, work: asyncio.Future[Any], deadline: float,
                          on_timeout: Callable[[], OneBotUnavailable]) -> Any:
        """在注入时钟期限内等待工作完成；未完成即取消工作并按给定类别报错（不重放）。

        取消与异常回收对本实例一切后台等待统一收口：动作与关闭两类等待共用，
        期限先到不等待不合作的取消。
        """

        timer = asyncio.create_task(self._sleep_until(deadline))
        try:
            await asyncio.wait((work, timer), return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                raise on_timeout()
            return work.result()
        finally:
            timer.cancel()
            work.cancel()
            timer.add_done_callback(_consume_exception)
            work.add_done_callback(_consume_exception)

    async def _execute(
        self, action: str, params: Mapping[str, Any],
        before_submit: Callable[[], bool] | None, deadline: float, state: _Submission,
    ) -> Mapping[str, Any]:
        async with self._lock:
            if self._closed:
                raise OneBotUnavailable("closed", submitted=False)
            if state.stopped or self._clock.now() >= deadline:
                raise OneBotUnavailable("timeout", submitted=False)
            body = self._request_body(action, params)
            token = self._request_token()
            if onebot_endpoint_rejection(self._connection.endpoint) is not None:
                raise OneBotUnavailable("configuration", submitted=False)
            if not await self._plaintext_target_allowed():
                raise OneBotUnavailable("insecure_endpoint", submitted=False)
            if before_submit is not None:
                try:
                    allowed = before_submit()
                except Exception:
                    allowed = False
                if not allowed:
                    raise OneBotUnavailable("permission", submitted=False)
            session = await self._ensure_session()
            try:
                state.submitted = True
                async with session.post(
                    self._action_url(action), data=body, allow_redirects=False,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                ) as response:
                    return await self._read_envelope(response, state)
            except OneBotUnavailable:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                # 断连、载荷错误与库异常一律按传输不可用；不回显异常携带的敏感载荷。
                raise OneBotUnavailable("transport_unavailable", submitted=state.submitted) from None

    def _request_body(self, action: str, params: Mapping[str, Any]) -> bytes:
        """动作信封序列化；超出自设上限时在发出前拒绝。"""

        body = json.dumps(
            {"action": action, "params": dict(params)}, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > ONEBOT_MAX_REQUEST_BYTES:
            raise OneBotUnavailable("request_too_large", submitted=False)
        return body

    def _request_token(self) -> str:
        """配置令牌优先；留空或全空白时才在发出请求前按名读取凭证环境变量。"""

        token = self._connection.token
        if isinstance(token, str) and token.strip():
            return token
        name = self._connection.token_env_var
        if not isinstance(name, str) or not valid_env_var_name(name):
            raise OneBotUnavailable("configuration", submitted=False)
        value = self._environ.get(name, "")
        if not isinstance(value, str) or not value.strip():
            raise OneBotUnavailable("credentials", submitted=False)
        return value

    def _action_url(self, action: str) -> str:
        """固定正向动作地址；令牌只经认证头，不进 URL。"""

        return f"{self._connection.endpoint.rstrip('/')}/{action}"

    async def _ensure_session(self) -> Any:
        """惰性复用单一会话：不经环境代理；重定向旗标逐请求显式关闭。"""

        if self._session is None or self._session.closed:
            import aiohttp

            factory = self._session_factory or aiohttp.ClientSession
            self._session = factory(trust_env=False)
        return self._session

    async def _plaintext_target_allowed(self) -> bool:
        """明文端点只允许回环字面量与解析为回环/私有/链路本地的目标。

        `https` 不做该判定（默认验证证书）；`http` 的主机名在发出请求前用运行循环
        解析，要求**全部**解析结果为可信网络。解析失败、结果含公网地址或没有任何
        结果一律拒绝，绝不因主机名（容器名）形态直接放行公网目标；`http` 公网 IP
        字面量已在配置判定中拒绝。这里只做判定，不写出任何请求、不携带凭证。
        """

        parts = urlsplit(self._connection.endpoint)
        if parts.scheme != "http":
            return True
        host = parts.hostname or ""
        if is_loopback_host(host):
            return True
        try:
            ipaddress.ip_address(host.rstrip("."))
        except ValueError:
            try:
                addresses = await self._resolver(host, parts.port if parts.port is not None else _DEFAULT_HTTP_PORT)
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            return bool(addresses) and all(is_private_host(str(address)) for address in addresses)
        return is_private_host(host)

    async def _read_envelope(self, response: Any, state: _Submission) -> Mapping[str, Any]:
        """状态分类后只认动作信封；3xx 与认证失败可证明未送达，其余失败保守对待。"""

        status = getattr(response, "status", 0)
        if 300 <= status < 400:
            raise OneBotUnavailable("insecure_endpoint", submitted=False)
        if status in (401, 403):
            raise OneBotUnavailable("authentication", submitted=False)
        if status >= 400:
            raise OneBotUnavailable("transport_unavailable", submitted=state.submitted)
        try:
            payload = await response.json(content_type=None)
        except (ValueError, TypeError):
            raise OneBotUnavailable("unexpected_response", submitted=state.submitted) from None
        if not _is_action_envelope(payload):
            raise OneBotUnavailable("unexpected_response", submitted=state.submitted)
        return payload
