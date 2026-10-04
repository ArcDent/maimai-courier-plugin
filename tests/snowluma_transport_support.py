"""直连传输的本地合成线路；不连接 QQ。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from types import SimpleNamespace

from aiohttp import WSMsgType, web


class CancellationGate:
    """可以拒绝取消的合成网络等待；测试结束后显式释放。"""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def wait(self):
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()


class MemorySocket:
    """合成网络库接缝；预缓冲事件用于确定性观测公平调度。"""

    def __init__(self, *, buffered_events=0, on_first_event=None):
        self.buffered_events = buffered_events
        self.events_read = 0
        self.on_first_event = on_first_event
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def receive(self):
        if self.events_read < self.buffered_events:
            self.events_read += 1
            if self.events_read == 1 and self.on_first_event is not None:
                asyncio.get_running_loop().call_soon(self.on_first_event)
            return SimpleNamespace(type=WSMsgType.TEXT, data='{"post_type":"message"}')
        return await self.frames.get()

    async def send_json(self, body):
        self.sent.append(body)

    async def close(self):
        self.closed = True
        self.frames.put_nowait(SimpleNamespace(type=WSMsgType.CLOSED, data=None))


class MemorySession:
    """仅实现网络库公开调用面，不访问传输内部状态。"""

    def __init__(self, socket):
        self.socket = socket
        self.connections = []
        self.closed = False

    async def ws_connect(self, url, **kwargs):
        self.connections.append((url, kwargs))
        return self.socket

    async def close(self):
        self.closed = True


async def spin_until(predicate):
    """只让出调度，有限轮询本地测试事件，不作真实长等待。"""
    for _ in range(4000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("本地测试事件未就绪")


@asynccontextmanager
async def endpoint(handler, *, ssl_context=None):
    """本机临时端口；处理函数与可观察线路由测试提供。"""
    app = web.Application()
    app.router.add_get("/onebot", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    scheme = "wss" if ssl_context else "ws"
    port = runner.addresses[0][1]
    try:
        yield f"{scheme}://127.0.0.1:{port}/onebot"
    finally:
        await runner.cleanup()


async def reply(ws, request, data):
    await ws.send_json({"status": "ok", "retcode": 0, "data": data, "echo": request["echo"]})
