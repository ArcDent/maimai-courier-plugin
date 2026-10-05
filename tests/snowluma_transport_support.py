"""直连 HTTP 传输的本地合成替身：可编程应答、内存会话与回环 HTTP 端点。

全部替身只经传输的公开注入点（`session_factory` / `resolver` / `clock` / `environ`）
进入，不访问传输内部状态，不连接真实 SnowLuma、不触公网。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web

from tests.support import envelope


class MemoryResponse:
    """可编程动作应答：状态、应答体、读取异常与挂起均可指定。"""

    def __init__(
        self, status: int = 200, *, body: Any = None,
        raises: BaseException | None = None, hang: bool = False,
    ) -> None:
        self.status = status
        self._body = envelope({}) if body is None else body
        self._raises = raises
        self._hang = hang
        self.released = False

    async def __aenter__(self) -> "MemoryResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self.released = True
        return False

    async def json(self, *, content_type: Any = None) -> Any:
        if self._hang:
            await asyncio.Event().wait()
        if self._raises is not None:
            raise self._raises
        return self._body


Responder = Callable[[str, bytes, dict[str, str], dict[str, Any]], Any]
"""替身应答入口：收到 URL、请求体、认证头与请求旗标，返回应答或抛出异常。"""


class MemorySession:
    """仅实现传输用到的网络库公开调用面；记录每个 POST 的 URL、体、头与旗标。

    `post` 与 aiohttp 同名方法一致地**同步**返回可作 `async with` 的应答对象
    （真实会话返回的是既可等待又是异步上下文管理器的请求上下文），替身因此
    不需要被 await 就能进入 `async with`。
    """

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.closed = False
        self.close_calls = 0
        self.posts: list[dict[str, Any]] = []

    def post(
        self, url: str, *, data: bytes | None = None, json: Any = None,
        headers: dict[str, str] | None = None, **kwargs: Any,
    ) -> Any:
        record: dict[str, Any] = {"url": url, "data": data, "headers": dict(headers or {}), "kwargs": dict(kwargs)}
        self.posts.append(record)
        outcome = self._responder(url, data or b"", record["headers"], record["kwargs"])
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class HangingSession(MemorySession):
    """`close()` 永不返回；验证关闭预算与幂等（取消时正常退出）。"""

    async def close(self) -> None:
        self.close_calls += 1
        await asyncio.Event().wait()


class RecordingFactory:
    """记录传输对惰性会话的构造参数；只经公开注入点进入。"""

    def __init__(self, session: MemorySession) -> None:
        self.session = session
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> MemorySession:
        self.calls.append(kwargs)
        return self.session


@asynccontextmanager
async def http_endpoint(handler: Callable[[web.Request], Any]):
    """本机临时回环 HTTP 端点；处理函数与记录由测试提供，零外部网络。"""

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()
