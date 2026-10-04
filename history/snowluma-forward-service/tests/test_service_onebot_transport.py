"""4.1 真实 aiohttp OneBot 传输：受控地址、凭证头与动作往返。

本用例只在**本机回环**上启动一个内存动作表的 SnowLuma 替身：不连接任何真实
OneBot/QQ、不发送任何真实消息。缺失 `aiohttp`（标准验证命令不装网络库）时整份
用例跳过；带 `--with aiohttp` 运行时给出真实连接证据。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

import pytest

pytest.importorskip("aiohttp", reason="真实 OneBot 传输用例需要 aiohttp（按需装载）")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from service.config import ServiceConfig  # noqa: E402
from service.onebot import (  # noqa: E402
    AiohttpOneBotTransport,
    OneBotConnection,
    OneBotUnavailable,
    resolve_onebot_connection,
)

TOKEN_ENV_VAR = "MC_TEST_ONEBOT_TOKEN"
TOKEN_VALUE = "synthetic-onebot-token"


class _LoopbackSnowLuma:
    """本机回环的 SnowLuma 替身：校验凭证头，按动作表应答，并记录收到的请求。"""

    def __init__(self) -> None:
        self.received: list[Mapping[str, Any]] = []
        self.authorized_requests = 0

    async def __aenter__(self) -> "_LoopbackSnowLuma":
        self._app = web.Application()
        self._app.router.add_get("/onebot", self._handler)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        host, port = self._runner.addresses[0][:2]
        self.endpoint = f"ws://{host}:{port}/onebot"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._runner.cleanup()

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN_VALUE}":
            raise web.HTTPUnauthorized()
        self.authorized_requests += 1
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            self.received.append(payload)
            await socket.send_json({"status": "ok", "retcode": 0, "echo": "platform-event"})
            await socket.send_json(
                {**self._action_result(str(payload.get("action") or "")), "echo": payload.get("echo")}
            )
        return socket

    def _action_result(self, action: str) -> Mapping[str, Any]:
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": 10000, "nickname": "搬运喵"}}
        if action == "send_msg":
            return {"status": "ok", "retcode": 0, "data": {"message_id": 424242}}
        return {"status": "failed", "retcode": 1404, "message": "unknown action"}


def _connection(endpoint: str, environ: Mapping[str, str]) -> OneBotConnection:
    config = ServiceConfig(
        account_id="10000",
        state_dir="/tmp/state",
        client_identity="client-test",
        onebot_endpoint=endpoint,
        onebot_token_env_var=TOKEN_ENV_VAR,
    )
    resolved = resolve_onebot_connection(config, environ)
    assert isinstance(resolved, OneBotConnection), resolved
    return resolved


def test_真实传输在回环上完成账号查询与群发送() -> None:
    async def scenario() -> None:
        async with _LoopbackSnowLuma() as server:
            transport = AiohttpOneBotTransport(
                _connection(server.endpoint, {TOKEN_ENV_VAR: TOKEN_VALUE}),
                environ={TOKEN_ENV_VAR: TOKEN_VALUE},
            )
            try:
                login = await transport.get_login_info()
                assert login["data"] == {"user_id": 10000, "nickname": "搬运喵"}

                receipt = await transport.send_msg(
                    target_group_id="20002",
                    message=[{"type": "text", "data": {"text": "你好"}}],
                )
                assert receipt["data"] == {"message_id": 424242}
            finally:
                await transport.close()

            assert len(server.received) == 2, server.received
            assert server.received[0]["action"] == "get_login_info"
            assert server.received[1]["params"]["group_id"] == "20002"
            assert server.received[1]["params"]["message"] == [
                {"type": "text", "data": {"text": "你好"}}
            ], "消息段原样交给网关，不改写"
            assert transport.endpoint == server.endpoint, "连接地址只取受控配置"

    asyncio.run(scenario())


def test_真实传输拒绝错误凭证与不可达地址() -> None:
    async def scenario() -> None:
        async with _LoopbackSnowLuma() as server:
            transport = AiohttpOneBotTransport(
                _connection(server.endpoint, {TOKEN_ENV_VAR: "wrong-token"}),
                environ={TOKEN_ENV_VAR: "wrong-token"},
            )
            try:
                with pytest.raises(OneBotUnavailable):
                    await transport.get_login_info()
            finally:
                await transport.close()

            assert server.authorized_requests == 0, "凭证错误时不得进入动作处理"

    asyncio.run(scenario())
