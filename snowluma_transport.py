"""SnowLuma 固定动作传输与纯配置解析；网络库延迟到建连时导入。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

ONEBOT_ACTION_TIMEOUT_SECONDS: Final[float] = 10.0
"""动作排队、握手、写入和响应共用的绝对期限。"""
ONEBOT_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
"""单独关闭的上限；上层可提前取消以共享更小的卸载剩余预算。"""


@dataclass(slots=True)
class _Submission:
    """本次调用的提交证据；取消后禁止尚未开始的写入。"""

    submitted: bool = False
    stopped: bool = False

if TYPE_CHECKING:
    from .config import CourierConfig
from .shared.clock import Clock, SystemClock
from .shared.config_rules import is_loopback_host, is_private_host, onebot_endpoint_rejection, valid_env_var_name

HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]
"""建连前的主机名解析注入点：返回全部解析地址文本，解析失败抛异常。"""


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
    """只解析受控配置，不建立连接、不读取环境变量中的凭证值。"""

    if isinstance(config, Mapping):
        from .config import CourierConfig

        try:
            config = CourierConfig(**dict(config))
        except ValueError as error:
            return OneBotUnresolved(str(error), config_failure=True)
    endpoint = config.snowluma.websocket_url
    if not endpoint:
        return OneBotUnresolved("未配置 SnowLuma WebSocket 地址；旧 [service] 配置已退役，不能作为直连配置")
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


class OneBotUnavailable(RuntimeError):
    """只暴露安全类别；submitted 为可能进入写入，默认保守为真。"""

    def __init__(self, category: str = "transport_unavailable", *, submitted: bool = True):
        self.category = category
        self.submitted = submitted
        super().__init__(f"OneBot 传输不可用（{category}）")


def _consume_exception(task: asyncio.Future[Any]) -> None:
    """收走本实例后台工作异常，不回显网络库携带的敏感载荷。"""
    if not task.cancelled():
        task.exception()


async def _reject_redirect(session: Any, context: Any, params: Any) -> None:
    """公开 trace 在下一跳前拒绝；不读取或回显 URL 与认证头。"""
    params.response.close()
    raise OneBotUnavailable("redirect", submitted=False)


@runtime_checkable
class OneBotTransport(Protocol):
    """仅供基础设施使用的四个固定动作；不提供任意动作代理。"""

    @property
    def connection_generation(self) -> int: ...

    async def get_login_info(self, *, expected_generation: int | None = None) -> Mapping[str, Any]: ...
    async def get_msg(self, message_id: str, *, expected_generation: int | None = None) -> Mapping[str, Any]: ...
    async def get_group_list(self, *, expected_generation: int | None = None) -> Mapping[str, Any]: ...
    async def send_msg(
        self, *, target_group_id: str, message: Sequence[Mapping[str, Any]],
        expected_generation: int, before_submit: Callable[[], bool], stage: str = "",
    ) -> Mapping[str, Any]: ...
    async def close(self) -> None: ...


class AiohttpOneBotTransport:
    """从原固定传输演进；构造不联网，单实例串行执行且不重放动作。"""

    def __init__(
        self, connection: OneBotConnection, *, clock: Clock | None = None,
        environ: Mapping[str, str] | None = None,
        register_task: Callable[[asyncio.Task[Any], str], None] | None = None,
        session_factory: Callable[..., Any] | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock if clock is not None else SystemClock()
        self._environ = environ if environ is not None else os.environ
        self._register_task = register_task
        self._session_factory = session_factory
        self._resolver = resolver if resolver is not None else _resolve_host_addresses
        self._lock = asyncio.Lock()
        self._socket: Any = None
        self._session: Any = None
        self._generation = 0
        self._sequence = 0
        self._instance = uuid.uuid4().hex
        self._receiver: asyncio.Task[None] | None = None
        self._pending: tuple[str, asyncio.Future[Mapping[str, Any]]] | None = None
        self._closed = False
        self._close_deadline: float | None = None
        self._cleanup_tasks: dict[asyncio.Task[Any], bool] = {}
        self._cleanup_failed = False
        self._operations: set[asyncio.Task[Any]] = set()
        self._closed_signal: asyncio.Future[None] | None = None

    @property
    def connection_generation(self) -> int:
        return self._generation

    async def get_login_info(self, *, expected_generation: int | None = None) -> Mapping[str, Any]:
        return await self._call("get_login_info", {}, expected_generation)

    async def get_msg(self, message_id: str, *, expected_generation: int | None = None) -> Mapping[str, Any]:
        return await self._call("get_msg", {"message_id": message_id}, expected_generation)

    async def get_group_list(self, *, expected_generation: int | None = None) -> Mapping[str, Any]:
        return await self._call("get_group_list", {}, expected_generation)

    async def send_msg(
        self, *, target_group_id: str, message: Sequence[Mapping[str, Any]],
        expected_generation: int, before_submit: Callable[[], bool], stage: str = "",
    ) -> Mapping[str, Any]:
        # stage 只定位基础设施工作，不写入协议。
        return await self._call("send_msg", {
            "message_type": "group", "group_id": target_group_id,
            "message": [dict(segment) for segment in message],
        }, expected_generation, before_submit, stage=stage)

    async def _plaintext_target_allowed(self) -> bool:
        """明文端点只允许回环字面量与解析为回环/私有/链路本地的目标（design D11）。

        `wss` 不做该判定；`ws` 的主机名在建立连接前用运行循环解析，要求**全部**
        解析结果为可信网络。解析失败、结果含公网地址或没有任何结果一律拒绝，绝不
        因主机名（容器名）形态直接放行公网目标；`ws` 公网 IP 字面量已在配置判定
        中拒绝。这里只做判定，不写出任何帧、不携带凭证。
        """

        parts = urlsplit(self._connection.endpoint)
        if parts.scheme != "ws":
            return True
        host = parts.hostname or ""
        if is_loopback_host(host):
            return True
        try:
            ipaddress.ip_address(host.rstrip("."))
        except ValueError:
            try:
                addresses = await self._resolver(host, parts.port if parts.port is not None else 80)
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            return bool(addresses) and all(is_private_host(str(address)) for address in addresses)
        return is_private_host(host)

    async def _connect(self, state: _Submission, deadline: float) -> Any:
        if (self._socket is not None and not self._socket.closed
                and self._receiver is not None and not self._receiver.done()):
            return self._socket
        self._start_connection_close()
        await self._wait_cleanup(deadline, wait_operations=False)
        if self._closed or state.stopped or self._clock.now() >= deadline:
            raise OneBotUnavailable("stopped", submitted=False)
        import aiohttp

        factory = self._session_factory or aiohttp.ClientSession
        category = "handshake"
        try:
            token = self._connection.token
            if (onebot_endpoint_rejection(self._connection.endpoint)
                    or not isinstance(token, str)
                    or (not token.strip() and not valid_env_var_name(self._connection.token_env_var))):
                raise OneBotUnavailable("configuration", submitted=False)
            if not await self._plaintext_target_allowed():
                raise OneBotUnavailable("insecure_endpoint", submitted=False)
            if not token.strip():
                token = self._environ.get(self._connection.token_env_var, "")
            if not isinstance(token, str) or not token.strip():
                raise OneBotUnavailable("credentials", submitted=False)
            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(_reject_redirect)
            self._session = factory(trust_env=False, trace_configs=[trace])
            socket = await self._session.ws_connect(
                self._connection.endpoint, headers={"Authorization": f"Bearer {token}"},
                ssl=True, compress=0, max_msg_size=4194304,
            )
            if self._closed or state.stopped:
                self._schedule_close(socket, "SnowLuma 关闭迟到连接")
                raise OneBotUnavailable("closed", submitted=False)
            self._socket = socket
        except OneBotUnavailable as error:
            category = error.category
        except aiohttp.ClientConnectorCertificateError:
            category = "certificate"
        except aiohttp.WSServerHandshakeError as error:
            category = "authentication" if error.status in (401, 403) else "handshake"
        except Exception:
            pass
        else:
            self._generation += 1
            self._receiver = asyncio.create_task(self._receive(self._socket, self._generation))
            if self._register_task is not None:
                self._register_task(self._receiver, "SnowLuma 接收")
            return self._socket
        self._start_connection_close()
        raise OneBotUnavailable(category, submitted=False) from None

    async def _call(
        self, action: str, params: Mapping[str, Any], expected_generation: int | None,
        before_submit: Callable[[], bool] | None = None, *, stage: str = "",
    ) -> Mapping[str, Any]:
        if self._closed:
            raise OneBotUnavailable("closed", submitted=False)
        deadline = self._clock.now() + ONEBOT_ACTION_TIMEOUT_SECONDS
        state = _Submission()
        if self._closed_signal is None:
            self._closed_signal = asyncio.get_running_loop().create_future()
        operation = asyncio.create_task(self._execute(action, params, expected_generation, before_submit, deadline, state))
        self._operations.add(operation)
        operation.add_done_callback(self._operations.discard)
        operation.add_done_callback(_consume_exception)
        timer = asyncio.create_task(self._wait_deadline(deadline))
        if self._register_task is not None:
            label = {"source": "源消息", "comment": "评论"}.get(stage, action)
            self._register_task(operation, f"SnowLuma {label}")
        try:
            await asyncio.wait((operation, timer, self._closed_signal), return_when=asyncio.FIRST_COMPLETED)
            if self._closed and not operation.done():
                raise OneBotUnavailable("closed", submitted=state.submitted)
            if self._clock.now() >= deadline:
                raise OneBotUnavailable("timeout", submitted=state.submitted)
            return operation.result()
        except asyncio.CancelledError:
            raise OneBotUnavailable("cancelled", submitted=state.submitted) from None
        except OneBotUnavailable as error:
            raise OneBotUnavailable(error.category, submitted=state.submitted) from None
        except Exception:
            raise OneBotUnavailable("transport", submitted=state.submitted) from None
        finally:
            state.stopped = True
            timer.cancel()
            operation.cancel()
            # 不等待拒绝取消的操作；关闭入口继续跟踪残留，回收异常不回显原文。
            timer.add_done_callback(_consume_exception)

    async def _wait_deadline(self, deadline: float) -> None:
        await self._clock.sleep(max(0.0, deadline - self._clock.now()))

    async def _execute(
        self, action: str, params: Mapping[str, Any], expected_generation: int | None,
        before_submit: Callable[[], bool] | None, deadline: float, state: _Submission,
    ) -> Mapping[str, Any]:
        async with self._lock:
            if self._closed:
                raise OneBotUnavailable("closed", submitted=False)
            if state.stopped or self._clock.now() >= deadline:
                raise OneBotUnavailable("timeout", submitted=False)
            if expected_generation is not None:
                if (expected_generation != self._generation or self._socket is None
                        or self._socket.closed or self._receiver is None or self._receiver.done()):
                    raise OneBotUnavailable("connection_changed", submitted=False)
                socket = self._socket
            else:
                socket = await self._connect(state, deadline)
            if self._closed:
                raise OneBotUnavailable("closed", submitted=False)
            if state.stopped or self._clock.now() >= deadline:
                raise OneBotUnavailable("timeout", submitted=False)
            self._sequence += 1
            echo = f"{self._instance}:{self._generation}:{self._sequence}"
            response = asyncio.get_running_loop().create_future()
            self._pending = (echo, response)
            try:
                if before_submit is not None:
                    try:
                        allowed = before_submit()
                    except Exception:
                        allowed = False
                    if not allowed:
                        raise OneBotUnavailable("permission", submitted=False)
                state.submitted = True
                await socket.send_json({"action": action, "params": dict(params), "echo": echo})
                return await response
            finally:
                self._pending = None
                response.cancel()
                _consume_exception(response)

    async def _receive(self, socket: Any, generation: int) -> None:
        """唯一接收者；空闲也消费事件，不保存消息、不为事件创建任务。"""
        from aiohttp import WSMsgType

        try:
            while True:
                # receive 在缓冲已有事件时可能不让出；主动让期限与卸载任务得到调度。
                await asyncio.sleep(0)
                frame = await socket.receive()
                if socket is not self._socket or generation != self._generation or frame.type != WSMsgType.TEXT:
                    break
                try:
                    payload = json.loads(frame.data)
                except (ValueError, TypeError):
                    continue
                pending = self._pending
                if (
                    pending is not None and isinstance(payload, dict)
                    and not any(key in payload for key in (
                        "post_type", "message_type", "meta_event_type", "notice_type", "request_type",
                    ))
                    and payload.get("echo") == pending[0]
                    and isinstance(payload.get("status"), str)
                    and payload["status"] in {"ok", "failed", "async"}
                    and type(payload.get("retcode")) is int
                    and "data" in payload
                    and isinstance(payload["data"], (dict, list, type(None)))
                    and not pending[1].done()
                ):
                    pending[1].set_result(payload)
        except Exception:
            pass
        finally:
            if socket is self._socket and generation == self._generation:
                self._generation += 1
                pending = self._pending
                if pending is not None and not pending[1].done():
                    pending[1].set_exception(OneBotUnavailable("disconnected"))

    def _start_connection_close(self) -> None:
        """同步失效旧核验；每个资源仅启动一次关闭，所有残留都可定位。"""
        socket, session, receiver = self._socket, self._session, self._receiver
        if socket is not None:
            self._generation += 1
        pending = self._pending
        if pending is not None and not pending[1].done():
            pending[1].set_exception(OneBotUnavailable("closed"))
        self._socket = self._session = self._receiver = None
        if receiver is not None:
            self._cleanup_tasks[receiver] = False
            receiver.add_done_callback(self._cleanup_done)
        for resource, description in [(socket, "SnowLuma 关闭连接"), (session, "SnowLuma 关闭会话")]:
            if resource is not None:
                self._schedule_close(resource, description)

    def _schedule_close(self, resource: Any, description: str) -> None:
        task = asyncio.create_task(resource.close())
        self._cleanup_tasks[task] = True
        task.add_done_callback(self._cleanup_done)
        if self._register_task is not None:
            self._register_task(task, description)

    def _cleanup_done(self, task: asyncio.Task[Any]) -> None:
        resource = self._cleanup_tasks.pop(task, False)
        # 资源关闭被取消不等于已释放；接收任务取消则是正常退出。
        if task.cancelled():
            self._cleanup_failed |= resource
        elif task.exception() is not None:
            self._cleanup_failed = True

    async def _wait_cleanup(self, deadline: float, *, wait_operations: bool = True) -> None:
        timer = asyncio.create_task(self._wait_deadline(deadline))
        try:
            while pending := {task for task in set(self._cleanup_tasks) | (self._operations if wait_operations else set()) if not task.done()}:
                if self._clock.now() >= deadline:
                    raise OneBotUnavailable("close_incomplete")
                await asyncio.wait((*pending, timer), return_when=asyncio.FIRST_COMPLETED)
            for task in tuple(self._cleanup_tasks):
                self._cleanup_done(task)
            if self._cleanup_failed:
                raise OneBotUnavailable("close_incomplete")
        except asyncio.CancelledError:
            raise OneBotUnavailable("close_incomplete") from None
        finally:
            timer.cancel()
            timer.add_done_callback(_consume_exception)
            for task in set(self._cleanup_tasks) | (self._operations if wait_operations else set()):
                if not task.done():
                    task.cancel()

    async def close(self) -> None:
        """幂等且有界；未退出工作保持登记，不发送注销或服务控制动作。"""
        self._closed = True
        if self._closed_signal is not None and not self._closed_signal.done():
            self._closed_signal.set_result(None)
        for operation in self._operations:
            operation.cancel()
        if self._close_deadline is None:
            self._close_deadline = self._clock.now() + ONEBOT_CLOSE_TIMEOUT_SECONDS
        self._start_connection_close()
        await self._wait_cleanup(self._close_deadline)
