"""固定 OneBot 传输接缝：受控地址、服务端凭证与环境变量（design D1/D5，4.1）。

- 连接地址只来自**受控服务配置**（`ServiceConfig.onebot_endpoint`），凭证只来自
  配置指定的**环境变量**；请求字段与模型参数都不能提供地址或凭证；
- `OneBotTransport` 是网关接缝的公开协议：`get_login_info` / `get_msg` /
  `get_group_list` / `send_msg`。测试注入 2.6 的模拟 OneBot（内存），真实实现是
  指向 SnowLuma 的 WebSocket 客户端，导入 `aiohttp` 延迟到真正建立连接时——
  服务导入期不加载网络库，依赖缺失时只在启动时明确失败；
- 传输失败一律抛 `OneBotUnavailable`（只带动作名与异常类型，不带正文、资源标识
  或凭证）；调用方不得据此换通路重发。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from .config import ServiceConfig, onebot_endpoint_rejection

ONEBOT_SESSION_TIMEOUT_SECONDS: Final[float] = 30.0
"""传输层单次调用之外的会话建立上界（秒）；阶段级超时由网关的注入时钟负责。"""


class OneBotUnavailable(RuntimeError):
    """OneBot 传输不可用：本次未取得回执，调用方必须按「结果不明」处理。

    只携带动作名与异常类型（可定位），不含任何消息正文、资源标识或凭证。
    """


@runtime_checkable
class OneBotTransport(Protocol):
    """网关接缝的公开传输面（形状对齐第 1 组核实的 OneBot 动作返回）。

    实现可以是真实 WebSocket 客户端（本模块）或测试替身；每个方法返回原始响应
    映射，成功与否由网关统一判定（`status` / `retcode` / `data`）。
    """

    async def get_login_info(self) -> Mapping[str, Any]:
        """只读账号查询：返回 `{user_id, nickname}`（形状经 1.2 核实）。"""

        ...

    async def get_msg(self, message_id: str) -> Mapping[str, Any]:
        """只读消息查询：返回存储副本；缓存缺失是明确失败响应。"""

        ...

    async def get_group_list(self) -> Mapping[str, Any]:
        """只读群列表查询：返回绑定账号当前可见的群（用于目标可用性核对）。"""

        ...

    async def send_msg(
        self,
        *,
        target_group_id: str,
        message: Sequence[Mapping[str, Any]],
        stage: str = "",
    ) -> Mapping[str, Any]:
        """向指定群发送一条消息；资源引用段只认 `forward.data.id`（1.2 核实）。

        `stage` 是**非协议必需字段**：只有调用归属与可观测性用途（
        `shared.send_steps.SEND_STAGE_SOURCE` / `SEND_STAGE_COMMENT`，缺省为空），
        供可控测试传输区分「源发送」与「评论发送」两个接缝；真实 WebSocket 实现
        显式忽略它、可以完全不带该参数，绝不写入协议载荷。
        """

        ...


@dataclass(frozen=True, slots=True)
class OneBotConnection:
    """已解析的受控连接描述：地址 + 凭证环境变量名（**不含凭证值**）。"""

    endpoint: str
    token_env_var: str


@dataclass(frozen=True, slots=True)
class OneBotUnresolved:
    """连接尚未可用：带可定位中文原因；`config_failure` 区分配置错误与未配置完成。"""

    reason: str
    config_failure: bool = False


def resolve_onebot_connection(
    config: ServiceConfig, environ: Mapping[str, str]
) -> OneBotConnection | OneBotUnresolved:
    """从受控配置与环境变量解析连接：地址只取配置、凭证只判存在性。

    地址校验（协议、主机名、用户信息、查询串、远程必须 wss）复用
    `service.config.onebot_endpoint_rejection` 的同一实现，不在本模块重写第二套。
    凭证值本身不进入返回值：`OneBotConnection` 只保存环境变量名，真实凭证在建立
    连接时由传输实现按名读取（不落盘、不回显、不写日志）。
    """

    endpoint = str(config.onebot_endpoint or "").strip()
    if not endpoint:
        return OneBotUnresolved(
            "未配置 OneBot 连接地址（服务尚未部署完成，不建立任何连接）",
            config_failure=False,
        )
    rejection = onebot_endpoint_rejection(endpoint)
    if rejection is not None:
        return OneBotUnresolved(rejection.reason, config_failure=True)

    token_env_var = str(config.onebot_token_env_var or "").strip()
    if not token_env_var:
        return OneBotUnresolved(
            "配置了 OneBot 连接地址就必须同时配置凭证环境变量名"
            "（连接只使用服务端受控凭证）",
            config_failure=True,
        )
    if not str(environ.get(token_env_var, "")).strip():
        return OneBotUnresolved(
            f"凭证环境变量 {token_env_var} 未设置或为空，拒绝建立 OneBot 连接",
            config_failure=True,
        )
    return OneBotConnection(endpoint=endpoint, token_env_var=token_env_var)


class AiohttpOneBotTransport:
    """指向固定 SnowLuma 地址的 OneBot WebSocket 客户端（`aiohttp` 延迟导入）。

    - 同一实例串行化调用（一把锁）：动作请求带唯一 `echo`，忽略平台推送事件与
      其它响应的干扰，直到读到本次 `echo` 的响应；
    - 连接断开或调用失败即丢弃当前会话，下一次调用按同一固定地址重连，不换地址、
      不换账号；
    - 不自行重试：一次调用对应一次平台动作（重发决策属于上层的结果分类）。
    """

    def __init__(self, connection: OneBotConnection, *, environ: Mapping[str, str] | None = None) -> None:
        self._connection = connection
        self._environ = environ if environ is not None else os.environ
        self._lock = asyncio.Lock()
        self._session: Any = None
        self._socket: Any = None
        self._echo = 0

    @property
    def endpoint(self) -> str:
        """受控连接地址（只读；解析后不再改变）。"""

        return self._connection.endpoint

    async def close(self) -> None:
        """关闭会话与连接（幂等；不发送任何消息）。"""

        socket, session = self._socket, self._session
        self._socket = None
        self._session = None
        if socket is not None and not bool(getattr(socket, "closed", True)):
            await socket.close()
        if session is not None and not bool(getattr(session, "closed", True)):
            await session.close()

    async def get_login_info(self) -> Mapping[str, Any]:
        return await self._call("get_login_info", {})

    async def get_msg(self, message_id: str) -> Mapping[str, Any]:
        return await self._call("get_msg", {"message_id": message_id})

    async def get_group_list(self) -> Mapping[str, Any]:
        return await self._call("get_group_list", {})

    async def send_msg(
        self,
        *,
        target_group_id: str,
        message: Sequence[Mapping[str, Any]],
        stage: str = "",
    ) -> Mapping[str, Any]:
        """发送群消息；`stage` 只是控制面提示，**不进入协议载荷**（同一固定动作）。

        它是非协议必需字段：只标识调用归属供可控传输/测试设施区分发送接缝，
        传输实现可忽略。
        """

        del stage  # 非协议必需字段：真实 OneBot 没有该字段，显式忽略、不改变线路参数
        return await self._call(
            "send_msg",
            {
                "message_type": "group",
                "group_id": target_group_id,
                "message": [dict(segment) for segment in message],
            },
        )

    async def _call(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """执行一次 OneBot 动作：唯一 `echo` 配对响应，失败不重试。"""

        async with self._lock:
            aiohttp = _import_aiohttp()
            try:
                socket = await self._connect(aiohttp)
                self._echo += 1
                echo = f"maimai-courier-{self._echo}"
                await socket.send_json({"action": action, "params": dict(params), "echo": echo})
                while True:
                    message = await socket.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise OneBotUnavailable(f"OneBot 连接在等待 {action} 响应时断开")
                    payload = json.loads(message.data)
                    if not isinstance(payload, Mapping):
                        continue
                    if str(payload.get("echo") or "") != echo:
                        continue  # 平台推送事件或无关响应：忽略，不据此判断结果
                    return payload
            except OneBotUnavailable:
                await self._drop()
                raise
            except asyncio.CancelledError:
                await self._drop()
                raise
            except Exception as error:
                await self._drop()
                raise OneBotUnavailable(
                    f"OneBot 动作 {action} 未取得响应（{type(error).__name__}），本次不重试"
                ) from error

    async def _connect(self, aiohttp: Any) -> Any:
        """取得可用连接：已有活连接直接复用，否则按固定地址重建。"""

        if self._session is not None and self._socket is not None and not bool(self._socket.closed):
            return self._socket
        await self.close()
        token = str(self._environ.get(self._connection.token_env_var, ""))
        if not token.strip():
            raise OneBotUnavailable(
                f"凭证环境变量 {self._connection.token_env_var} 未设置或为空，拒绝建立连接"
            )
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=ONEBOT_SESSION_TIMEOUT_SECONDS)
        )
        try:
            socket = await session.ws_connect(
                self._connection.endpoint, headers={"Authorization": f"Bearer {token}"}
            )
        except asyncio.CancelledError:
            await session.close()
            raise
        except Exception as error:
            await session.close()
            raise OneBotUnavailable(
                f"OneBot 连接建立失败（{type(error).__name__}），地址只取受控配置"
            ) from error
        self._session, self._socket = session, socket
        return socket

    async def _drop(self) -> None:
        """丢弃当前会话（下一次调用按同一固定地址重连）。"""

        socket, session = self._socket, self._session
        self._socket = None
        self._session = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


def _import_aiohttp() -> Any:
    """延迟导入网络库：服务导入期不加载 aiohttp（2.3 的导入期约束）。"""

    try:
        import aiohttp
    except ImportError as error:  # pragma: no cover - 依赖缺失只在启动时出现
        raise OneBotUnavailable(
            "缺少 aiohttp 依赖（aiohttp>=3.14.3），无法建立 OneBot 连接"
        ) from error
    return aiohttp


async def close_transport(transport: OneBotTransport) -> None:
    """关闭传输（实现提供 `close` 时）：不发送任何消息，幂等。

    供服务启动/退出与测试夹具统一调用；测试替身没有 `close` 时直接返回。
    """

    close = getattr(transport, "close", None)
    if not callable(close):
        return
    outcome = close()
    if isinstance(outcome, Awaitable):
        await outcome


def build_onebot_transport(
    config: ServiceConfig, *, environ: Mapping[str, str] | None = None
) -> AiohttpOneBotTransport | OneBotUnresolved:
    """按受控配置构造真实传输；地址/凭证不合法时返回可定位的未就绪原因。"""

    resolved = resolve_onebot_connection(config, environ if environ is not None else os.environ)
    if isinstance(resolved, OneBotUnresolved):
        return resolved
    return AiohttpOneBotTransport(resolved, environ=environ)
