"""独立 HTTP 服务的装配与生命周期（design D1/D4/D5/D9，第 5 组）。

- `build_running_service`：按受控配置装配认证门、会话注册表与协调器（纯 Python，不联网）；
- `start_http_server`：真实 aiohttp 传输，导入延迟到真正启动时；依赖缺失或监听失败时
  **明确返回不可用**，不静默降级、也不假装已提供服务；
- `RunningHttpServer.stop`：停止监听并释放端口（幂等）；
- `RunningService`：应用 + 协调器 + 监听信息 + 受控配置，供 `start` 的服务循环与测试
  接缝（`ServeSeam`）共同使用。

本模块不做任何平台发送：发送只发生在协调器经 `service.gateway` 调用时。
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final, Mapping

from shared.clock import Clock
from shared.ledger import LedgerRepository

from .auth import EnvTokenAuthGate
from .config import ServiceConfig
from .coordinator import ServiceCoordinator
from .gateway import SnowLumaGateway
from .http_api import MAX_BODY_BYTES, HttpRequest, ServiceApp
from .sessions import SessionRegistry

logger = logging.getLogger(__name__)
"""模块日志器：只记录停止监听/清理失败的异常原因（不记录凭证、地址以外的请求数据）。"""

SERVE_EXIT_OK: Final[int] = 0
"""服务循环正常退出（收到停止信号）的退出码。"""


@dataclass(frozen=True, slots=True)
class ServerUnavailable:
    """服务端不可用：带可定位中文原因；调用方必须如实报告，不假装已提供服务。"""

    reason: str


@dataclass
class RunningHttpServer:
    """已启动的真实 HTTP 服务端：监听地址、端口与停止入口。"""

    app: ServiceApp
    host: str
    port: int
    _runner: Any = None
    _site: Any = None

    @property
    def address(self) -> str:
        """实际监听地址（含端口；端口为 0 时取系统分配的真实端口）。"""

        return f"{self.host}:{self.port}"

    async def stop(self) -> None:
        """停止监听并释放端口（幂等；不发送任何消息）。

        停止或清理失败不静默吞掉：记录可定位原因（端口可能未释放，调用方据此判断）。
        """

        site, runner = self._site, self._runner
        self._site, self._runner = None, None
        if site is not None:
            try:
                await site.stop()
            except Exception as error:
                logger.warning(
                    "停止 HTTP 监听失败（%s）：端口可能未释放", type(error).__name__
                )
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as error:
                logger.warning(
                    "清理 HTTP 运行器失败（%s）：连接与端口可能未完全释放",
                    type(error).__name__,
                )


@dataclass
class RunningService:
    """装配好的服务：HTTP 应用 + 协调器 + 监听信息 + 受控配置。"""

    app: ServiceApp
    coordinator: ServiceCoordinator
    config: ServiceConfig
    gateway: SnowLumaGateway
    clock: Clock
    ledger: LedgerRepository
    host: str = ""
    port: int = 0

    @property
    def address(self) -> str:
        """对外监听地址（尚未启动 HTTP 时为空串）。"""

        return f"{self.host}:{self.port}" if self.host else ""


ServeSeam = Callable[[RunningService], Awaitable[int]]
"""服务运行接缝：默认等待停止信号；测试注入「做一件事就停」的替身，不修改服务逻辑。"""


def build_running_service(
    *,
    config: ServiceConfig,
    ledger: LedgerRepository,
    gateway: SnowLumaGateway,
    clock: Clock,
    ledger_identity: str,
    environ: Mapping[str, str] | None = None,
) -> RunningService | ServerUnavailable:
    """按受控配置装配认证门、会话注册表与协调器；缺少凭证时明确不可用。"""

    environment = os.environ if environ is None else environ
    auth = EnvTokenAuthGate(
        token_env_var=config.token_env_var,
        subject=config.client_identity,
        environ=environment,
    )
    if not auth.credential_available:
        return ServerUnavailable(
            f"凭证环境变量 {config.token_env_var} 未设置或为空：拒绝装配服务端（不建立任何发送）"
        )
    sessions = SessionRegistry(
        clock=clock,
        account_id=config.account_id,
        ledger_identity=ledger_identity,
        server_generation=f"gen-{os.urandom(6).hex()}",
        server_source_groups=config.allowed_source_groups,
        server_target_groups=config.allowed_target_groups,
        account_generation=gateway.account_generation,
    )
    coordinator = ServiceCoordinator(
        ledger=ledger,
        gateway=gateway,
        sessions=sessions,
        clock=clock,
        bound_subject=config.client_identity,
    )
    return RunningService(
        app=ServiceApp(backends=coordinator, auth=auth),
        coordinator=coordinator,
        config=config,
        gateway=gateway,
        clock=clock,
        ledger=ledger,
    )


def _import_aiohttp() -> Any | None:
    """延迟导入网络库：服务导入期不加载 aiohttp（2.3 的导入期约束）。

    依赖缺失返回 `None`（由调用方转成明确的不可用原因），不抛异常、不静默降级；
    显式导入 `aiohttp.web` 子模块（aiohttp 不会在导入根包时自动加载它）。
    """

    try:
        import aiohttp
        import aiohttp.web  # noqa: F401 - 显式加载子模块，供 web.Application 使用
    except ImportError:  # pragma: no cover - 依赖缺失只在启动时出现
        return None
    return aiohttp


def _ssl_context(config: ServiceConfig) -> Any:
    """按受控配置构造 TLS 上下文（证书校验开启；跨主机监听由配置校验强制要求）。"""

    if not config.tls_cert_file or not config.tls_key_file:
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(config.tls_cert_file, config.tls_key_file)
    return context


async def start_http_server(
    app: ServiceApp, config: ServiceConfig
) -> RunningHttpServer | ServerUnavailable:
    """在受控地址上启动真实 HTTP 服务端（默认回环；跨主机必须 TLS）。"""

    aiohttp = _import_aiohttp()
    if aiohttp is None:
        return ServerUnavailable(
            "缺少 aiohttp 依赖（aiohttp>=3.14.3），无法启动 HTTP 服务端"
        )
    web = aiohttp.web

    async def handle(request: Any) -> Any:
        """把 aiohttp 请求交给纯 Python 应用：认证、校验与分发都在应用层完成。"""

        body = await request.content.read(MAX_BODY_BYTES + 1)
        response = await app.handle(
            HttpRequest(
                method=request.method,
                path=request.path_qs,
                headers=dict(request.headers),
                body=body,
            )
        )
        return web.json_response(
            dict(response.payload),
            status=response.status,
            dumps=lambda payload: json.dumps(payload, ensure_ascii=False),
        )

    application = web.Application()
    application.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(application)
    try:
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=config.listen_host,
            port=config.listen_port,
            ssl_context=_ssl_context(config),
        )
        await site.start()
    except Exception as error:
        await runner.cleanup()
        return ServerUnavailable(f"HTTP 服务端启动失败（{type(error).__name__}），未对外提供接口")
    bound = _bound_address(runner, config)
    if bound is None:
        await runner.cleanup()
        return ServerUnavailable(
            "HTTP 服务端已监听但无法确认系统分配的真实端口：如实报告不可用，不伪造地址"
        )
    return RunningHttpServer(app=app, host=bound[0], port=bound[1], _runner=runner, _site=site)


def _bound_address(runner: Any, config: ServiceConfig) -> tuple[str, int] | None:
    """取实际绑定的地址与端口：只读运行器的公开地址列表（端口 0 由系统分配）。

    读不到真实地址时，只有受控配置本身就带非 0 端口才回退（回退值与请求值一致，
    不是静默错报）；配置端口为 0 时返回 `None`，由调用方明确报告不可用——绝不把
    0 当成系统分配的真实端口。
    """

    for host, port in getattr(runner, "addresses", ()) or ():
        if isinstance(host, str) and host and isinstance(port, int) and port > 0:
            return host, int(port)
    if config.listen_port > 0:
        return config.listen_host, config.listen_port
    return None
