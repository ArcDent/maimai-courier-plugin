"""第3组：仅经公开传输方法与本地合成 WebSocket 验收。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("aiohttp")
from aiohttp import WSMsgType, web

from maimai_courier_plugin import snowluma_transport as transport
from tests.snowluma_transport_support import CancellationGate, MemorySession, MemorySocket, endpoint, reply, spin_until
from tests.support import FakeClock


def client(url, **kwargs):
    return transport.AiohttpOneBotTransport(
        transport.OneBotConnection(url, "TEST_ONEBOT_TOKEN"),
        environ={"TEST_ONEBOT_TOKEN": "synthetic-not-a-production-token"},
        **kwargs,
    )


@pytest.mark.parametrize("token, reference, environ, expected", [
    ("synthetic-config-token", "", {}, "synthetic-config-token"),
    ("synthetic-config-token", "TEST_ONEBOT_TOKEN", {"TEST_ONEBOT_TOKEN": "unused"}, "synthetic-config-token"),
    ("", "TEST_ONEBOT_TOKEN", {"TEST_ONEBOT_TOKEN": "synthetic-env-token"}, "synthetic-env-token"),
    (" \t\n", "TEST_ONEBOT_TOKEN", {"TEST_ONEBOT_TOKEN": "synthetic-env-token"}, "synthetic-env-token"),
])
def test_配置令牌与环境变量按优先级用于真实握手(token, reference, environ, expected):
    async def scenario():
        received = []
        async def handler(request):
            received.append(request.headers.get("Authorization"))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                await reply(ws, json.loads(frame.data), {"user_id": "10000"})
            return ws
        async with endpoint(handler) as url:
            connection = transport.resolve_onebot_connection({"snowluma": {
                "websocket_url": url, "token": token, "token_env_var": reference,
            }}, environ)
            assert isinstance(connection, transport.OneBotConnection)
            t = transport.AiohttpOneBotTransport(connection, environ=environ)
            try:
                assert (await t.get_login_info())["data"] == {"user_id": "10000"}
            finally:
                await t.close()
        assert received == [f"Bearer {expected}"]
    asyncio.run(scenario())


def test_配置令牌认证失败不回显也不改用环境凭证(caplog):
    async def scenario():
        seen = []
        async def handler(request):
            seen.append(request.headers.get("Authorization"))
            return web.Response(status=401, text="synthetic-config-secret")
        async with endpoint(handler) as url:
            t = transport.AiohttpOneBotTransport(
                transport.OneBotConnection(url, "TEST_ONEBOT_TOKEN", "synthetic-config-secret"),
                environ={"TEST_ONEBOT_TOKEN": "synthetic-env-secret"},
            )
            try:
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await t.get_login_info()
                assert caught.value.category == "authentication" and not caught.value.submitted
                assert "synthetic-config-secret" not in repr(caught.value) + caplog.text
                assert "synthetic-env-secret" not in repr(caught.value) + caplog.text
            finally:
                await t.close()
        assert seen == ["Bearer synthetic-config-secret"]
    asyncio.run(scenario())


def test_3_1_无SDK导入且构造不联网并完成固定只读动作():
    program = '''
import importlib, sys, types
pkg = types.ModuleType("isolated_courier")
pkg.__path__ = [sys.argv[1]]
sys.modules[pkg.__name__] = pkg
m = importlib.import_module("isolated_courier.snowluma_transport")
t = m.AiohttpOneBotTransport(m.OneBotConnection("ws://127.0.0.1:1/onebot", "FAKE_TOKEN"), environ={})
assert "maibot_sdk" not in sys.modules
assert "aiohttp" not in sys.modules
assert not any(n.startswith(("service", "history")) for n in sys.modules)
'''
    result = subprocess.run([sys.executable, "-I", "-c", program, str(Path(__file__).resolve().parents[1])], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    async def scenario():
        actions = []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                body = json.loads(frame.data)
                actions.append((body["action"], body["params"]))
                await reply(ws, body, {"sample": body["action"]})
            return ws
        async with endpoint(handler) as url:
            t = client(url)
            assert actions == []
            try:
                assert isinstance(t, transport.OneBotTransport)
                assert (await t.get_login_info())["data"] == {"sample": "get_login_info"}
                assert (await t.get_msg("source-123"))["data"] == {"sample": "get_msg"}
                assert (await t.get_group_list())["data"] == {"sample": "get_group_list"}
            finally:
                await t.close()
        assert actions == [("get_login_info", {}), ("get_msg", {"message_id": "source-123"}), ("get_group_list", {})]
    asyncio.run(scenario())


def test_3_3_伪装事件和非法响应不能完成动作且空闲消费心跳():
    async def scenario():
        bad_sent, release, pong = asyncio.Event(), asyncio.Event(), asyncio.Event()
        registered, actions = [], []
        async def handler(request):
            ws = web.WebSocketResponse(autoping=False)
            await ws.prepare(request)
            body = json.loads((await ws.receive()).data)
            actions.append(body)
            good = {"status": "ok", "retcode": 0, "data": {"user_id": "10000"}, "echo": body["echo"]}
            for payload in [
                {**good, "post_type": "message", "message_type": "group"},
                {**good, "post_type": "meta_event", "meta_event_type": "heartbeat"},
                {**good, "post_type": "message_sent"},
                {**good, "echo": "other-client"},
                {"status": "ok", "retcode": 0, "data": {}},
                {"echo": body["echo"]},
                {**good, "retcode": True}, {**good, "status": []},
                {**good, "data": "not-a-response-object"}, [],
            ]:
                await ws.send_json(payload)
            await ws.send_str("not-json")
            bad_sent.set()
            await release.wait()
            await ws.send_json(good)
            await ws.ping(b"idle-heartbeat")
            async for frame in ws:
                if frame.type == WSMsgType.PONG:
                    pong.set()
                else:
                    actions.append(frame.data)
            return ws
        async with endpoint(handler) as url:
            t = client(url, register_task=lambda task, description: registered.append((task, description)))
            task = asyncio.create_task(t.get_login_info())
            try:
                await bad_sent.wait()
                for _ in range(30):
                    await asyncio.sleep(0)
                assert not task.done()
                release.set()
                assert (await task)["data"] == {"user_id": "10000"}
                await spin_until(pong.is_set)
                assert len(actions) == 1
                assert len([task for task, _ in registered if not task.done()]) == 1
            finally:
                release.set()
                await t.close()
            assert all(task.done() for task, _ in registered)
    asyncio.run(scenario())


def test_3_4_双客户端广播和过期响应隔离且关闭互不影响():
    async def scenario():
        clock = FakeClock()
        sockets, requests = [], []
        release = asyncio.Event()
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            async for frame in ws:
                body = json.loads(frame.data)
                requests.append((ws, body))
                if body["action"] == "get_login_info":
                    await reply(ws, body, {"client": len(sockets)})
                elif body["action"] == "get_msg":
                    continue
                else:
                    # 服务器广播其他实例及旧动作响应；这些载荷不能完成本次动作。
                    for _, previous in requests[:-1]:
                        await reply(ws, previous, {"wrong": True})
                    await release.wait()
                    await reply(ws, body, {"groups": "current"})
            return ws
        async with endpoint(handler) as url:
            a, b = client(url, clock=clock), client(url)
            try:
                assert (await a.get_login_info())["data"] == {"client": 1}
                assert (await b.get_login_info())["data"] == {"client": 2}
                old_generation = a.connection_generation
                stale = asyncio.create_task(a.get_msg("timeout-source"))
                await spin_until(lambda: len(requests) == 3)
                clock.advance(10)
                with pytest.raises(transport.OneBotUnavailable):
                    await stale
                await sockets[0].close()
                await spin_until(lambda: a.connection_generation != old_generation)
                assert (await a.get_login_info())["data"] == {"client": 3}
                current = asyncio.create_task(a.get_group_list())
                await spin_until(lambda: len(requests) == 5)
                for _ in range(30):
                    await asyncio.sleep(0)
                assert not current.done()
                release.set()
                assert (await current)["data"] == {"groups": "current"}
                await a.close()
                assert (await b.get_group_list())["data"] == {"groups": "current"}
                echoes = [body["echo"] for _, body in requests]
                assert len(set(echoes)) == len(echoes)
                assert {body["action"] for _, body in requests} == {"get_login_info", "get_msg", "get_group_list"}
            finally:
                release.set()
                await a.close()
                await b.close()
    asyncio.run(scenario())


def test_3_5_握手排队响应共用十秒绝对期限():
    async def scenario():
        clock = FakeClock()
        handshake, open_handshake = asyncio.Event(), asyncio.Event()
        actions = []
        async def handler(request):
            handshake.set()
            await open_handshake.wait()
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                actions.append(json.loads(frame.data))
            return ws
        async with endpoint(handler) as url:
            t = client(url, clock=clock)
            first = asyncio.create_task(t.get_login_info())
            await handshake.wait()
            second = asyncio.create_task(t.get_group_list())
            try:
                await spin_until(lambda: len(clock.sleeps) >= 2)
                clock.advance(9)
                open_handshake.set()
                await spin_until(lambda: len(actions) == 1)
                clock.advance(1)
                await spin_until(lambda: first.done() and second.done())
                for task, submitted in [(first, True), (second, False)]:
                    with pytest.raises(transport.OneBotUnavailable) as caught:
                        await task
                    assert caught.value.category == "timeout"
                    assert caught.value.submitted is submitted
                assert len(actions) == 1
            finally:
                open_handshake.set()
                first.cancel()
                second.cancel()
                await t.close()
                await asyncio.gather(first, second, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["handshake", "events"])
def test_3_5_握手阻塞及持续事件均不能延长期限(phase):
    async def scenario():
        clock = FakeClock()
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def handler(request):
            entered.set()
            if phase == "handshake":
                await release.wait()
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                requests.append(json.loads(frame.data))
                for _ in range(100):
                    await ws.send_json({"post_type": "meta_event", "meta_event_type": "heartbeat"})
            return ws
        async with endpoint(handler) as url:
            t = client(url, clock=clock)
            task = asyncio.create_task(t.get_login_info())
            try:
                await entered.wait()
                if phase == "events":
                    await spin_until(lambda: bool(requests))
                clock.advance(10)
                await spin_until(task.done)
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await task
                assert caught.value.category == "timeout"
                assert caught.value.submitted is (phase == "events")
            finally:
                release.set()
                await t.close()
    asyncio.run(scenario())


def test_3_6_缓冲事件洪泛公平让出且不扩张后台任务():
    async def scenario():
        clock = FakeClock()
        observed, registered = [], []
        def progress():
            observed.append(socket.events_read)
            clock.advance(10)
        socket = MemorySocket(buffered_events=5000, on_first_event=progress)
        session = MemorySession(socket)
        t = client("ws://127.0.0.1:1/onebot", clock=clock,
                   session_factory=lambda **kwargs: session,
                   register_task=lambda task, desc: registered.append(task))
        try:
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await t.get_login_info()
            assert caught.value.category == "timeout"
            assert 0 < observed[0] < 5000
            assert len(registered) <= 4
            assert len(socket.sent) == 1
        finally:
            await t.close()
    asyncio.run(scenario())


def test_3_6_并发调用仅有一个待决动作():
    async def scenario():
        requests = []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                requests.append((ws, json.loads(frame.data)))
            return ws
        async with endpoint(handler) as url:
            t = client(url)
            tasks = [asyncio.create_task(t.get_msg(str(index))) for index in range(8)]
            try:
                for index in range(8):
                    await spin_until(lambda: len(requests) >= index + 1)
                    for _ in range(20):
                        await asyncio.sleep(0)
                    assert len(requests) == index + 1
                    ws, body = requests[index]
                    await reply(ws, body, {"message_id": body["params"]["message_id"]})
                results = await asyncio.gather(*tasks)
                assert [r["data"]["message_id"] for r in results] == [str(i) for i in range(8)]
            finally:
                await t.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fragmented", [False, True])
def test_3_6_完整消息超过四MiB关闭且提交事实不丢失(fragmented):
    async def scenario():
        closed = asyncio.Event()
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive()
            if fragmented:
                # RFC 6455 文本首片与 continuation 尾片；每片未超限，合计超过4MiB。
                import struct
                for opcode, size in [(0x01, 2097152), (0x80, 2097153)]:
                    request.transport.write(bytes([opcode, 127]) + struct.pack("!Q", size) + b"x" * size)
            else:
                try:
                    await ws.send_str("x" * 4194305)
                except ConnectionError:
                    # 对端可在读到超限消息头后立即关闭，不必接收完整载荷。
                    closed.set()
                    return ws
            async for _ in ws:
                pass
            closed.set()
            return ws
        async with endpoint(handler) as url:
            t = client(url)
            try:
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await t.get_login_info()
                assert caught.value.submitted is True
                await spin_until(closed.is_set)
            finally:
                await t.close()
    asyncio.run(scenario())


def test_3_7_排队后的许可复验拒绝且固定连接不重连():
    async def scenario():
        actions, sockets = [], []
        held, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            async for frame in ws:
                body = json.loads(frame.data)
                actions.append(body)
                if body["action"] == "get_msg":
                    held.set()
                    await release.wait()
                await reply(ws, body, {"user_id": "10000"})
            return ws
        async with endpoint(handler) as url:
            t = client(url)
            await t.get_login_info()
            generation = t.connection_generation
            permitted = True
            checks = []
            def allowed():
                checks.append(permitted)
                return permitted
            blocker = asyncio.create_task(t.get_msg("held"))
            await held.wait()
            send = asyncio.create_task(t.send_msg(target_group_id="20000", message=[], expected_generation=generation, before_submit=allowed))
            try:
                for _ in range(20):
                    await asyncio.sleep(0)
                assert checks == []
                permitted = False
                release.set()
                await blocker
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await send
                assert caught.value.submitted is False
                assert checks == [False]
                assert all(body["action"] != "send_msg" for body in actions)
                await sockets[0].close()
                await spin_until(lambda: t.connection_generation != generation)
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await t.get_group_list(expected_generation=generation)
                assert caught.value.submitted is False
                assert len(sockets) == 1
                await t.get_login_info()
                assert len(sockets) == 2
                assert t.connection_generation != generation
            finally:
                release.set()
                await t.close()
                await asyncio.gather(blocker, send, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
@pytest.mark.parametrize("failure", ["disconnect", "timeout", "cancel"])
def test_3_7_提交后故障保留证据且后续只读不重放(stage, failure):
    async def scenario():
        clock = FakeClock()
        sockets, actions, descriptions = [], [], []
        received = asyncio.Event()
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            async for frame in ws:
                body = json.loads(frame.data)
                actions.append(body)
                if body["action"] == "send_msg":
                    received.set()
                    if failure == "disconnect":
                        request.transport.abort()
                else:
                    await reply(ws, body, {"user_id": "10000"})
            return ws
        async with endpoint(handler) as url:
            t = client(url, clock=clock, register_task=lambda task, description: descriptions.append(description))
            try:
                await t.get_login_info()
                generation = t.connection_generation
                task = asyncio.create_task(t.send_msg(
                    target_group_id="20000", message=[{"type": "text", "data": {"text": "合成内容"}}],
                    expected_generation=generation, before_submit=lambda: True, stage=stage))
                await received.wait()
                if failure == "timeout":
                    clock.advance(10)
                elif failure == "cancel":
                    task.cancel()
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await task
                assert caught.value.submitted is True
                await sockets[0].close()
                await spin_until(lambda: t.connection_generation != generation)
                await t.get_login_info()
                assert len(sockets) == 2
                sends = [body for body in actions if body["action"] == "send_msg"]
                assert len(sends) == 1
                assert sends[0]["params"] == {"message_type": "group", "group_id": "20000", "message": [{"type": "text", "data": {"text": "合成内容"}}]}
                assert "stage" not in sends[0]
                assert {"source": "SnowLuma 源消息", "comment": "SnowLuma 评论"}[stage] in descriptions
            finally:
                await t.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["queued_cancel", "callback_error"])
def test_3_7_排队取消或许可异常证明未提交且不泄露(failure):
    async def scenario():
        actions = []
        held, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                body = json.loads(frame.data)
                actions.append(body)
                if body["action"] == "get_msg":
                    held.set()
                    await release.wait()
                await reply(ws, body, {})
            return ws
        async with endpoint(handler) as url:
            t = client(url)
            try:
                await t.get_login_info()
                blocker = asyncio.create_task(t.get_msg("held"))
                await held.wait()
                def denied():
                    raise RuntimeError("synthetic-secret-source-content")
                task = asyncio.create_task(t.send_msg(target_group_id="20000", message=[],
                    expected_generation=t.connection_generation, before_submit=denied))
                for _ in range(10):
                    await asyncio.sleep(0)
                if failure == "queued_cancel":
                    task.cancel()
                release.set()
                await blocker
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await task
                assert caught.value.submitted is False
                assert "synthetic-secret-source-content" not in str(caught.value)
                assert [body["action"] for body in actions] == ["get_login_info", "get_msg"]
            finally:
                release.set()
                await t.close()
    asyncio.run(scenario())


def test_3_8_关闭终止待决且幂等并永久禁止新动作():
    async def scenario():
        requests, connections, registered = [], [], []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            connections.append(ws)
            async for frame in ws:
                requests.append(json.loads(frame.data))
            return ws
        async with endpoint(handler) as url:
            t = client(url, register_task=lambda task, description: registered.append(task))
            task = asyncio.create_task(t.get_login_info())
            try:
                await spin_until(lambda: bool(requests))
                await t.close()
                await spin_until(task.done)
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await task
                assert caught.value.submitted is True
                await t.close()
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await t.get_login_info()
                assert caught.value.submitted is False
                assert len(connections) == 1
                assert len(requests) == 1
                assert all(task.done() for task in registered)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await t.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["receive", "socket_close", "session_close"])
def test_3_8_不合作清理共享五秒上界且如实报告残留(blocked, caplog):
    async def scenario():
        clock, gate = FakeClock(), CancellationGate()
        registered = []
        class Socket(MemorySocket):
            async def receive(self):
                if blocked == "receive":
                    await gate.wait()
                return await super().receive()
            async def close(self):
                if blocked == "socket_close":
                    await gate.wait()
                await super().close()
        class Session(MemorySession):
            async def close(self):
                if blocked == "session_close":
                    await gate.wait()
                await super().close()
        socket = Socket()
        t = client("ws://127.0.0.1:1/onebot", clock=clock,
                   session_factory=lambda **kwargs: Session(socket),
                   register_task=lambda task, description: registered.append(task))
        action = asyncio.create_task(t.get_login_info())
        await spin_until(lambda: bool(socket.sent))
        closing = asyncio.create_task(t.close())
        try:
            await gate.entered.wait()
            # 让关闭预算登记；不是把真实网络库的 ws_close 当作全部预算。
            for _ in range(20):
                await asyncio.sleep(0)
            clock.advance(5)
            await spin_until(closing.done)
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await closing
            assert caught.value.category == "close_incomplete"
            await spin_until(action.done)
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await action
            assert caught.value.submitted is True
            assert any(not task.done() for task in registered)
            before = len(socket.sent)
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await t.get_group_list()
            assert caught.value.submitted is False
            assert len(socket.sent) == before
        finally:
            gate.release.set()
            await asyncio.gather(action, closing, return_exceptions=True)
            await spin_until(lambda: all(task.done() for task in registered))
            await t.close()
        assert all(task.done() for task in registered)
    asyncio.run(scenario())
    assert "synthetic-not-a-production-token" not in caplog.text


@pytest.mark.parametrize("blocked", ["handshake", "write"])
def test_3_8_不合作握手或写入取消后保持证据且迟到资源关闭(blocked, caplog):
    async def scenario():
        from types import SimpleNamespace
        clock, gate = FakeClock(), CancellationGate()
        registered, loop_errors = [], []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: loop_errors.append(context))
        class Socket(MemorySocket):
            async def send_json(self, body):
                await super().send_json(body)
                if body["action"] == "send_msg":
                    await gate.wait()
                    raise RuntimeError("synthetic-sensitive-late-write")
                self.frames.put_nowait(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(
                    {"status": "ok", "retcode": 0, "data": {}, "echo": body["echo"]})))
        class Session(MemorySession):
            async def ws_connect(self, url, **kwargs):
                if blocked == "handshake":
                    await gate.wait()
                return await super().ws_connect(url, **kwargs)
        socket = Socket()
        session = Session(socket)
        t = client("ws://127.0.0.1:1/onebot", clock=clock,
                   session_factory=lambda **kwargs: session,
                   register_task=lambda task, description: registered.append(task))
        if blocked == "write":
            await t.get_login_info()
            action = asyncio.create_task(t.send_msg(target_group_id="20000", message=[],
                expected_generation=t.connection_generation, before_submit=lambda: True))
        else:
            action = asyncio.create_task(t.get_login_info())
        await gate.entered.wait()
        closing = asyncio.create_task(t.close())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            clock.advance(5)
            await spin_until(closing.done)
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await closing
            assert caught.value.category == "close_incomplete"
            await spin_until(action.done)
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await action
            assert caught.value.submitted is (blocked == "write")
            assert any(not task.done() for task in registered)
        finally:
            gate.release.set()
            clock.advance(10)
            await asyncio.gather(action, closing, return_exceptions=True)
            await spin_until(lambda: all(task.done() for task in registered))
            await t.close()
        assert socket.closed and session.closed
        assert len([body for body in socket.sent if body["action"] == "send_msg"]) == (1 if blocked == "write" else 0)
        assert loop_errors == []
    asyncio.run(scenario())
    assert "synthetic-sensitive-late-write" not in caplog.text


def test_3_8_非法帧失效后独立只读重连并关闭旧连接():
    async def scenario():
        sockets, actions = [], []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            async for frame in ws:
                body = json.loads(frame.data)
                actions.append(body)
                if len(sockets) == 1:
                    await ws.send_bytes(b"illegal-response")
                else:
                    await reply(ws, body, {"user_id": "10000"})
            return ws
        async with endpoint(handler) as url:
            t = client(url)
            second = None
            try:
                with pytest.raises(transport.OneBotUnavailable):
                    await t.get_login_info()
                second = asyncio.create_task(t.get_login_info())
                await spin_until(lambda: len(actions) == 2)
                assert len(sockets) == 2
                assert (await second)["data"] == {"user_id": "10000"}
                await spin_until(lambda: sockets[0].closed)
            finally:
                await t.close()
                if second is not None:
                    await asyncio.gather(second, return_exceptions=True)
    asyncio.run(scenario())


def test_3_8_旧清理未退出不建立新会话或积压关闭任务():
    async def scenario():
        clock, gate = FakeClock(), CancellationGate()
        sessions, registered = [], []
        class Session(MemorySession):
            async def ws_connect(self, url, **kwargs):
                raise RuntimeError("synthetic-handshake-detail")
            async def close(self):
                await gate.wait()
                await super().close()
        def factory(**kwargs):
            session = Session(MemorySocket())
            sessions.append(session)
            return session
        t = client("ws://127.0.0.1:1/onebot", clock=clock, session_factory=factory,
                   register_task=lambda task, description: registered.append(task))
        second = None
        try:
            with pytest.raises(transport.OneBotUnavailable):
                await t.get_login_info()
            await gate.entered.wait()
            second = asyncio.create_task(t.get_login_info())
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(sessions) == 1
            clock.advance(10)
            await spin_until(second.done)
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await second
            assert caught.value.submitted is False
            assert len(sessions) == 1
        finally:
            gate.release.set()
            clock.advance(10)
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)
            await spin_until(lambda: all(task.done() for task in registered))
            await t.close()
    asyncio.run(scenario())


def test_3_8_外层提前取消关闭不重置预算或伪称资源已释放():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Socket(MemorySocket):
            async def close(self):
                entered.set()
                await release.wait()
                await super().close()
        socket = Socket()
        t = client("ws://127.0.0.1:1/onebot", session_factory=lambda **kwargs: MemorySession(socket))
        action = asyncio.create_task(t.get_login_info())
        await spin_until(lambda: bool(socket.sent))
        closing = asyncio.create_task(t.close())
        try:
            await entered.wait()
            closing.cancel()
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await closing
            assert caught.value.category == "close_incomplete"
            await asyncio.gather(action, return_exceptions=True)
            with pytest.raises(transport.OneBotUnavailable):
                await t.close()
            assert not socket.closed
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await t.get_login_info()
            assert caught.value.submitted is False
        finally:
            release.set()
            await socket.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing", "empty", "auth", "tls", "redirect", "exception"])
def test_3_2_握手失败安全分类且重定向零请求(failure, tmp_path):
    async def scenario():
        seen, redirected = [], []
        async def destination(request):
            redirected.append(dict(request.headers))
            return web.Response(status=403)
        async with endpoint(destination) as other:
            async def handler(request):
                seen.append(request.headers.get("Authorization"))
                if failure == "redirect":
                    raise web.HTTPFound(location=other.replace("ws://", "http://"))
                if failure == "auth":
                    return web.Response(status=401, text="Authorization: Bearer synthetic-not-a-production-token")
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for frame in ws:
                    await reply(ws, json.loads(frame.data), {})
                return ws
            tls = None
            if failure == "tls":
                import ssl
                key, cert = tmp_path / "synthetic.key", tmp_path / "synthetic.crt"
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.load_cert_chain(cert, key)
            async with endpoint(handler, ssl_context=tls) as url:
                env = {} if failure == "missing" else {"TEST_ONEBOT_TOKEN": "   " if failure == "empty" else "synthetic-not-a-production-token"}
                def broken_factory(**kwargs):
                    raise RuntimeError("Authorization: Bearer synthetic-not-a-production-token " + url)
                t = transport.AiohttpOneBotTransport(transport.OneBotConnection(url, "TEST_ONEBOT_TOKEN"), environ=env, session_factory=broken_factory if failure == "exception" else None)
                try:
                    with pytest.raises(transport.OneBotUnavailable) as caught:
                        await t.get_login_info()
                    assert caught.value.submitted is False
                    assert caught.value.category == {"missing": "credentials", "empty": "credentials", "auth": "authentication", "tls": "certificate", "redirect": "redirect", "exception": "handshake"}[failure]
                    assert "synthetic-not-a-production-token" not in str(caught.value)
                    assert url not in str(caught.value)
                    assert caught.value.__cause__ is None
                    assert redirected == []
                    if failure in {"missing", "empty", "tls", "exception"}:
                        assert seen == []
                finally:
                    await t.close()
    asyncio.run(scenario())


# --------------------------------------------------------------------------- 9.2
# 明文端点只允许回环字面量与解析为回环/私有/链路本地的目标（design D11）；
# 名字在建连前解析，解析失败或含公网地址一律拒绝且不建立连接。


async def _no_dns(host, port):
    raise AssertionError("该路径不得查询解析器")


def test_9_2_容器名解析为私有时允许明文建连且连接原地址():
    async def scenario():
        socket, resolved = MemorySocket(), []
        session = MemorySession(socket)

        async def resolver(host, port):
            resolved.append((host, port))
            return ["10.20.30.40"]

        t = client("ws://maimai-snowluma:3001/onebot", resolver=resolver,
                   session_factory=lambda **kwargs: session)
        task = asyncio.create_task(t.get_login_info())
        try:
            await spin_until(lambda: bool(socket.sent))
            assert resolved == [("maimai-snowluma", 3001)]
            assert [body["action"] for body in socket.sent] == ["get_login_info"]
            assert [url for url, _ in session.connections] == ["ws://maimai-snowluma:3001/onebot"]
        finally:
            await t.close()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("address", ["127.0.0.1", "10.20.30.40", "fe80::1%en0"])
def test_9_2_名字解析结果全为可信网络时才放行(address):
    async def scenario():
        socket = MemorySocket()
        session = MemorySession(socket)

        async def resolver(host, port):
            return [address]

        t = client("ws://maimai-snowluma:3001/onebot", resolver=resolver,
                   session_factory=lambda **kwargs: session)
        task = asyncio.create_task(t.get_group_list())
        try:
            await spin_until(lambda: bool(socket.sent))
            assert len(session.connections) == 1
        finally:
            await t.close()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["public", "mixed", "failure", "empty"])
def test_9_2_公网混合或解析失败拒绝且无连接无发送帧(outcome):
    async def scenario():
        created = []

        def factory(**kwargs):
            created.append(kwargs)
            raise AssertionError("解析未通过时不得创建网络会话")

        async def resolver(host, port):
            if outcome == "failure":
                raise OSError("synthetic-resolution-sensitive-detail")
            if outcome == "public":
                return ["93.184.216.34"]
            if outcome == "mixed":
                return ["10.20.30.40", "93.184.216.34"]
            return []

        t = client("ws://maimai-snowluma:3001/onebot", resolver=resolver, session_factory=factory)
        try:
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await t.get_login_info()
            assert caught.value.category == "insecure_endpoint"
            assert caught.value.submitted is False
            assert caught.value.__cause__ is None
            assert "synthetic-resolution-sensitive-detail" not in str(caught.value)
            assert created == []
            assert t.connection_generation == 0
        finally:
            await t.close()
    asyncio.run(scenario())


def test_9_2_回环明文不查询解析器且真实线路可用():
    async def scenario():
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                await reply(ws, json.loads(frame.data), {"user_id": "10000"})
            return ws

        async with endpoint(handler) as url:
            t = client(url, resolver=_no_dns)
            try:
                assert (await t.get_login_info())["data"] == {"user_id": "10000"}
            finally:
                await t.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["ws://10.11.12.13:3001/onebot", "ws://[fd00::1]:3001/onebot",
                                "ws://[fe80::1]:3001/onebot"])
def test_9_2_私有字面量明文不查询解析器(url):
    async def scenario():
        socket = MemorySocket()
        session = MemorySession(socket)
        t = client(url, resolver=_no_dns, session_factory=lambda **kwargs: session)
        task = asyncio.create_task(t.get_group_list())
        try:
            await spin_until(lambda: bool(socket.sent))
            assert len(session.connections) == 1
        finally:
            await t.close()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_9_2_wss不受明文目标判定影响():
    async def scenario():
        socket = MemorySocket()
        session = MemorySession(socket)
        t = client("wss://snowluma.example/onebot", resolver=_no_dns,
                   session_factory=lambda **kwargs: session)
        task = asyncio.create_task(t.get_login_info())
        try:
            await spin_until(lambda: bool(socket.sent))
            assert len(session.connections) == 1
        finally:
            await t.close()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["ws://8.8.8.8:3001/onebot", "ws://[::ffff:8.8.8.8]:3001/onebot"])
def test_9_2_公网字面量明文在传输层也不建连(url):
    async def scenario():
        created = []

        def factory(**kwargs):
            created.append(kwargs)
            raise AssertionError("公网明文不得创建网络会话")

        t = client(url, resolver=_no_dns, session_factory=factory)
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await t.get_login_info()
        assert caught.value.submitted is False
        assert created == []
        assert t.connection_generation == 0
        await t.close()
    asyncio.run(scenario())
