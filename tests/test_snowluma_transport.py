"""固定动作 HTTP 传输：安全闸门、错误分类与关闭语义。

断言面只经公开导出（`OneBotTransport` / `OneBotUnresolved` / `OneBotUnavailable` /
`AiohttpOneBotTransport`）与注入点（`session_factory` / `resolver` / `clock` /
`environ`），不访问传输内部状态，不触真实网络。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aiohttp")
import aiohttp
from aiohttp import web

from maimai_courier_plugin import snowluma_transport as transport
from tests.snowluma_transport_support import (
    HangingSession, MemoryResponse, MemorySession, RecordingFactory, http_endpoint,
)
from tests.support import FakeClock, envelope, spin_until

ENDPOINT = "http://127.0.0.1:3000"
"""替身固定回环地址；连接由回环端点或会话替身承担，不触公网。"""

CONNECTION_TOKEN = "synthetic-config-token"
"""合成配置令牌；真实凭证绝不进入测试或仓库。"""


def memory_transport(
    session: MemorySession, *, connection: transport.OneBotConnection | None = None,
    clock: FakeClock | None = None, resolver: Any = None, environ: dict[str, str] | None = None,
) -> tuple[transport.AiohttpOneBotTransport, RecordingFactory]:

    factory = RecordingFactory(session)
    client = transport.AiohttpOneBotTransport(
        connection or transport.OneBotConnection(ENDPOINT, "TEST_ONEBOT_TOKEN", CONNECTION_TOKEN),
        clock=clock, environ=environ if environ is not None else {},
        session_factory=factory, resolver=resolver,
    )
    return client, factory


def test_解析只读受控配置() -> None:

    empty = transport.resolve_onebot_connection({"snowluma": {"http_url": ""}}, {})
    assert isinstance(empty, transport.OneBotUnresolved) and not empty.config_failure

    wrong_scheme = transport.resolve_onebot_connection({"snowluma": {"http_url": "ws://127.0.0.1:3001/onebot"}}, {})
    assert isinstance(wrong_scheme, transport.OneBotUnresolved) and wrong_scheme.config_failure

    public = transport.resolve_onebot_connection({"snowluma": {"http_url": "http://8.8.8.8:3000"}}, {})
    assert isinstance(public, transport.OneBotUnresolved) and public.config_failure

    missing = transport.resolve_onebot_connection(
        {"snowluma": {"http_url": "http://127.0.0.1:3000", "token": "", "token_env_var": "TEST_ONEBOT_TOKEN"}}, {},
    )
    assert isinstance(missing, transport.OneBotUnresolved) and missing.config_failure

    resolved = transport.resolve_onebot_connection(
        {"snowluma": {"http_url": "http://127.0.0.1:3000", "token": CONNECTION_TOKEN, "token_env_var": ""}}, {},
    )
    assert isinstance(resolved, transport.OneBotConnection)
    assert resolved.endpoint == "http://127.0.0.1:3000" and resolved.token == CONNECTION_TOKEN
    assert CONNECTION_TOKEN not in repr(resolved), "连接描述不得回显令牌"

    from_env = transport.resolve_onebot_connection(
        {"snowluma": {"http_url": "http://127.0.0.1:3000", "token": " \t\n", "token_env_var": "TEST_ONEBOT_TOKEN"}},
        {"TEST_ONEBOT_TOKEN": "synthetic-env-token"},
    )
    assert isinstance(from_env, transport.OneBotConnection)
    assert from_env.token == "" and from_env.token_env_var == "TEST_ONEBOT_TOKEN"


@pytest.mark.parametrize("token, env_var, environ, expected", [
    (CONNECTION_TOKEN, "", {}, CONNECTION_TOKEN),
    (CONNECTION_TOKEN, "TEST_ONEBOT_TOKEN", {"TEST_ONEBOT_TOKEN": "synthetic-env-secret"}, CONNECTION_TOKEN),
    (" \t\n", "TEST_ONEBOT_TOKEN", {"TEST_ONEBOT_TOKEN": "synthetic-env-token"}, "synthetic-env-token"),
    ("", "TEST_ONEBOT_TOKEN", {"TEST_ONEBOT_TOKEN": "synthetic-env-token"}, "synthetic-env-token"),
])
def test_配置令牌优先且空白仅在请求前回退环境变量(token, env_var, environ, expected) -> None:

    async def scenario() -> None:
        received: list[str | None] = []

        async def handler(request: web.Request) -> web.Response:
            received.append(request.headers.get("Authorization"))
            return web.json_response(envelope({"user_id": "10000"}))

        async with http_endpoint(handler) as url:
            client = transport.AiohttpOneBotTransport(
                transport.OneBotConnection(url, env_var, token), environ=environ,
            )
            try:
                assert (await client.get_login_info())["data"] == {"user_id": "10000"}
            finally:
                await client.close()
        assert received == [f"Bearer {expected}"]

    asyncio.run(scenario())


@pytest.mark.parametrize("token, env_var, category", [
    (" \t", "TEST_ONEBOT_TOKEN", "credentials"),
    (" \t", "1INVALID", "configuration"),
])
def test_凭证缺失或环境变量名非法在发出请求前拒绝(token, env_var, category) -> None:
    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200))
        client, _ = memory_transport(
            session, connection=transport.OneBotConnection(ENDPOINT, env_var, token), environ={},
        )
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await client.get_login_info()
        assert caught.value.category == category and not caught.value.submitted
        assert session.posts == [], "凭证未就绪不得发出任何请求"

    asyncio.run(scenario())


def _raising_gate() -> bool:
    raise RuntimeError("synthetic-recheck-failure")


@pytest.mark.parametrize("gate", [lambda: False, _raising_gate])
def test_提交前许可复验失败则明确未发送(gate) -> None:

    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200))
        client, _ = memory_transport(session)
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await client.send_msg(target_group_id="20002", message=[{"type": "text", "data": {"text": "synthetic-body"}}],
                                  before_submit=gate)
        assert caught.value.category == "permission" and not caught.value.submitted
        assert session.posts == [], "许可复验失败不得写出任何请求"

    asyncio.run(scenario())


def test_固定七动作逐动作经HTTP调用且信封与令牌仅经认证头() -> None:

    async def scenario() -> None:
        seen: list[dict[str, Any]] = []
        card_node = {"type": "node", "data": {"sender": "10000", "name": "synthetic-sender",
                                              "content": [{"type": "text", "data": {"text": "synthetic-card-node"}}]}}

        async def handler(request: web.Request) -> web.Response:
            body = await request.json()
            seen.append({
                "method": request.method,
                "path": request.path,
                "query": request.query_string,
                "body": body,
                "authorization": request.headers.get("Authorization"),
            })
            return web.json_response(envelope({"action": body["action"]}))

        async with http_endpoint(handler) as url:
            client = transport.AiohttpOneBotTransport(
                transport.OneBotConnection(url, "TEST_ONEBOT_TOKEN", CONNECTION_TOKEN),
                environ={"TEST_ONEBOT_TOKEN": "synthetic-env-secret"},
            )
            try:
                assert isinstance(client, transport.OneBotTransport)
                assert (await client.get_login_info())["data"] == {"action": "get_login_info"}
                assert (await client.get_group_list())["data"] == {"action": "get_group_list"}
                assert (await client.get_msg("source-123"))["data"] == {"action": "get_msg"}
                assert (await client.get_forward_msg("forward-res-123"))["data"] == {"action": "get_forward_msg"}
                assert (await client.send_msg(
                    target_group_id="20002",
                    message=[{"type": "text", "data": {"text": "synthetic-body"}}],
                    before_submit=lambda: True,
                ))["data"] == {"action": "send_msg"}
                assert (await client.send_group_forward_msg(
                    target_group_id="20002", messages=[card_node], before_submit=lambda: True,
                ))["data"] == {"action": "send_group_forward_msg"}
                assert (await client.send_forward_msg(
                    target_group_id="20002",
                    messages=[{"type": "node", "data": {"uin": "10000", "name": "synthetic-sender",
                                                        "content": [{"type": "text", "data": {"text": "synthetic-body"}}]}}],
                    before_submit=lambda: True,
                ))["data"] == {"action": "send_forward_msg"}
            finally:
                await client.close()

        assert [item["path"] for item in seen] == [
            "/get_login_info", "/get_group_list", "/get_msg", "/get_forward_msg",
            "/send_msg", "/send_group_forward_msg", "/send_forward_msg",
        ]
        assert all(item["method"] == "POST" for item in seen)
        assert [item["body"] for item in seen] == [
            {"action": "get_login_info", "params": {}},
            {"action": "get_group_list", "params": {}},
            {"action": "get_msg", "params": {"message_id": "source-123"}},
            {"action": "get_forward_msg", "params": {"id": "forward-res-123"}},
            {"action": "send_msg", "params": {"message_type": "group", "group_id": "20002",
                                              "message": [{"type": "text", "data": {"text": "synthetic-body"}}]}},
            {"action": "send_group_forward_msg", "params": {"group_id": "20002", "messages": [card_node]}},
            {"action": "send_forward_msg", "params": {"group_id": "20002", "messages": [
                {"type": "node", "data": {"uin": "10000", "name": "synthetic-sender",
                                          "content": [{"type": "text", "data": {"text": "synthetic-body"}}]}}]}},
        ]
        assert all(item["authorization"] == f"Bearer {CONNECTION_TOKEN}" for item in seen)
        assert all(item["query"] == "" for item in seen)
        assert all(CONNECTION_TOKEN not in item["path"] + item["query"] for item in seen)

    asyncio.run(scenario())


def test_只读展开查询get_forward_msg经内存替身调用() -> None:
    """卡片展开查询：`id` 参数只读信封，应答节点序列原样上交。"""

    async def scenario() -> None:
        nodes = [{"sender": {"user_id": "10000"},
                  "message": [{"type": "text", "data": {"text": "synthetic-card-node"}}]}]
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(
            200, body=envelope({"messages": nodes})))
        client, _ = memory_transport(session)
        response = await client.get_forward_msg("synthetic-card-res-id")
        assert response["data"] == {"messages": nodes}, "展开节点序列原样上交，不做改写"
        post = session.posts[0]
        assert post["url"].endswith("/get_forward_msg")
        assert json.loads(post["data"]) == {"action": "get_forward_msg", "params": {"id": "synthetic-card-res-id"}}
        assert post["headers"]["Authorization"] == f"Bearer {CONNECTION_TOKEN}"
        assert post["kwargs"]["allow_redirects"] is False

    asyncio.run(scenario())


def test_卡片发送send_group_forward_msg提交节点信封且许可复验拦截() -> None:
    """卡片重组投递：节点数组原样进信封、不设预览参数、提交前许可复验。"""

    async def scenario() -> None:
        node = {"type": "node", "data": {"uin": "10000", "name": "synthetic-sender",
                                         "content": [{"type": "text", "data": {"text": "synthetic-card-node"}}]}}
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(
            200, body=envelope({"message_id": -1489176197, "res_id": "synthetic-res-id",
                                "forward_id": "synthetic-forward-id"})))
        client, _ = memory_transport(session)
        response = await client.send_group_forward_msg(
            target_group_id="20002", messages=[node], before_submit=lambda: True)
        assert response["data"]["message_id"] == -1489176197
        post = session.posts[0]
        assert post["url"].endswith("/send_group_forward_msg")
        body = json.loads(post["data"])
        assert body == {"action": "send_group_forward_msg",
                        "params": {"group_id": "20002", "messages": [node]}}
        assert set(body["params"]) == {"group_id", "messages"}, "不得设置 summary/news/prompt/source 预览类参数"

        with pytest.raises(transport.OneBotUnavailable) as caught:
            await client.send_group_forward_msg(
                target_group_id="20002", messages=[node], before_submit=lambda: False)
        assert caught.value.category == "permission" and not caught.value.submitted
        assert len(session.posts) == 1, "许可复验失败不得写出任何请求"

    asyncio.run(scenario())


def test_会话惰性创建且禁代理禁重定向旗标随请求发出() -> None:

    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200, body=envelope({"user_id": "10000"})))
        client, factory = memory_transport(session)
        assert factory.calls == [], "构造与首个动作前不得创建会话"
        assert (await client.get_login_info())["data"] == {"user_id": "10000"}
        assert factory.calls == [{"trust_env": False}]
        assert session.posts[0]["kwargs"]["allow_redirects"] is False
        await client.get_group_list()
        assert factory.calls == [{"trust_env": False}], "会话惰性复用，不逐请求新建"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_3xx重定向显式拒绝且不向下一跳发出请求或凭证(status) -> None:

    async def scenario() -> None:
        seen: list[dict[str, Any]] = []

        async def handler(request: web.Request) -> web.Response:
            seen.append({"path": request.path, "authorization": request.headers.get("Authorization")})
            if request.path == "/get_login_info":
                return web.Response(status=status, headers={"Location": "/redirect-leak"})
            return web.json_response(envelope({"leaked": True}))

        async with http_endpoint(handler) as url:
            client = transport.AiohttpOneBotTransport(
                transport.OneBotConnection(url, "TEST_ONEBOT_TOKEN", CONNECTION_TOKEN), environ={},
            )
            try:
                with pytest.raises(transport.OneBotUnavailable) as caught:
                    await client.get_login_info()
                assert caught.value.category == "insecure_endpoint" and not caught.value.submitted
            finally:
                await client.close()
        assert [item["path"] for item in seen] == ["/get_login_info"], "不得向重定向下一跳发出请求"
        assert CONNECTION_TOKEN not in "/redirect-leak"

    asyncio.run(scenario())


def test_请求体超过自设上限在发出前拒绝() -> None:

    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200))
        client, factory = memory_transport(session)
        overhead = len(json.dumps(
            {"action": "send_msg", "params": {"message_type": "group", "group_id": "20002",
                                              "message": [{"type": "text", "data": {"text": ""}}]}},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        pad = transport.ONEBOT_MAX_REQUEST_BYTES - overhead
        message = [{"type": "text", "data": {"text": "x" * pad}}]
        assert (await client.send_msg(
            target_group_id="20002", message=message, before_submit=lambda: True,
        ))["status"] == "ok"
        assert len(session.posts) == 1
        assert len(session.posts[0]["data"]) == transport.ONEBOT_MAX_REQUEST_BYTES

        with pytest.raises(transport.OneBotUnavailable) as caught:
            await client.send_msg(
                target_group_id="20002",
                message=[{"type": "text", "data": {"text": "x" * (pad + 1)}}],
                before_submit=lambda: True,
            )
        assert caught.value.category == "request_too_large" and not caught.value.submitted
        assert len(session.posts) == 1, "超限请求体不得发出"
        assert factory.calls == [{"trust_env": False}], "超限拒绝不得为此新建会话"

    asyncio.run(scenario())


@pytest.mark.parametrize("host, outcome, expect, resolver_called", [
    ("8.8.8.8", None, "configuration", False),
    ("snowluma.example", ["8.8.8.8"], "insecure_endpoint", True),
    ("snowluma.example", ["10.1.2.3"], None, True),
    ("snowluma.example", ["127.0.0.1", "10.1.2.3"], None, True),
    ("snowluma.example", ["192.168.1.5", "8.8.8.8"], "insecure_endpoint", True),
    ("snowluma.example", [], "insecure_endpoint", True),
    ("snowluma.example", "raise", "insecure_endpoint", True),
    ("127.0.0.1", "forbid", None, False),
    ("10.0.0.5", "forbid", None, False),
])
def test_明文http目标建连前解析判定(host, outcome, expect, resolver_called) -> None:

    async def scenario() -> None:
        calls: list[tuple[str, int]] = []

        async def resolver(name: str, port: int) -> list[str]:
            calls.append((name, port))
            if outcome == "raise":
                raise OSError("synthetic-resolution-failure")
            return list(outcome)

        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200))
        client, _ = memory_transport(
            session, connection=transport.OneBotConnection(f"http://{host}:3000", "TEST_ONEBOT_TOKEN", CONNECTION_TOKEN),
            resolver=resolver,
        )
        if expect is None:
            await client.get_login_info()
            assert len(session.posts) == 1
        else:
            with pytest.raises(transport.OneBotUnavailable) as caught:
                await client.get_login_info()
            assert caught.value.category == expect
            assert session.posts == [], "判定失败不得发出任何请求"
        assert bool(calls) is resolver_called
        assert calls == ([(host, 3000)] if resolver_called else [])

    asyncio.run(scenario())


def test_加密端点不执行明文解析判定() -> None:
    async def scenario() -> None:
        async def resolver(name: str, port: int) -> list[str]:
            raise AssertionError("https 目标不做明文网络判定")

        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200))
        client, _ = memory_transport(
            session,
            connection=transport.OneBotConnection("https://snowluma.example:3000", "TEST_ONEBOT_TOKEN", CONNECTION_TOKEN),
            resolver=resolver,
        )
        assert (await client.get_login_info())["status"] == "ok"

    asyncio.run(scenario())


@pytest.mark.parametrize("status, failure, category, submitted", [
    (401, None, "authentication", False),
    (403, None, "authentication", False),
    (429, None, "transport_unavailable", True),
    (500, None, "transport_unavailable", True),
    (200, "post-disconnect", "transport_unavailable", True),
    (200, "post-reset", "transport_unavailable", True),
    (200, "read-payload", "transport_unavailable", True),
])
def test_状态与连接失败按安全类别分类(status, failure, category, submitted) -> None:

    async def scenario() -> None:
        def responder(url: str, body: bytes, headers: dict[str, str], kwargs: dict[str, Any]) -> Any:
            if failure == "post-disconnect":
                raise aiohttp.ServerDisconnectedError()
            if failure == "post-reset":
                raise ConnectionResetError("synthetic-reset")
            if failure == "read-payload":
                return MemoryResponse(status, raises=aiohttp.ClientPayloadError())
            return MemoryResponse(status)

        session = MemorySession(responder)
        client, _ = memory_transport(session)
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await client.get_msg("source-1")
        assert caught.value.category == category
        assert caught.value.submitted is submitted

    asyncio.run(scenario())


@pytest.mark.parametrize("retcode", [100, 1400, 1404])
def test_业务失败信封原样交上层结构化处理(retcode) -> None:

    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(
            200, body=envelope(None, status="failed", retcode=retcode, message="message not found"),
        ))
        client, _ = memory_transport(session)
        response = await client.get_msg("missing-1")
        assert response["status"] == "failed" and response["retcode"] == retcode
        assert len(session.posts) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("body, raises", [
    ({"hello": "synthetic"}, None),
    ({"post_type": "message", "message_type": "group", "status": "ok", "retcode": 0, "data": {}}, None),
    ({"status": "ok", "retcode": 0, "data": "synthetic-scalar"}, None),
    ({"status": "partial", "retcode": 0, "data": {}}, None),
    (None, ValueError("synthetic-not-json")),
])
def test_非信封响应不构成动作结果(body, raises) -> None:

    async def scenario() -> None:
        session = MemorySession(lambda url, url_body, headers, kwargs: MemoryResponse(200, body=body, raises=raises))
        client, _ = memory_transport(session)
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await client.get_msg("source-1")
        assert caught.value.category == "unexpected_response"
        assert caught.value.submitted is True, "请求已写入，结果只能保守对待"

    asyncio.run(scenario())


def test_动作期限到期归类超时且不重发() -> None:

    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200, hang=True))
        clock = FakeClock()
        client, _ = memory_transport(session, clock=clock)
        call = asyncio.create_task(client.get_login_info())
        await spin_until(lambda: len(session.posts) == 1)
        assert clock.sleeps, "期限必须走注入时钟登记"
        clock.advance(transport.ONEBOT_ACTION_TIMEOUT_SECONDS + 0.5)
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await call
        assert caught.value.category == "timeout" and caught.value.submitted
        assert len(session.posts) == 1, "超时不重发同一动作"

    asyncio.run(scenario())


def test_关闭幂等且重复调用不重复关闭会话() -> None:
    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200))
        client, _ = memory_transport(session)
        await client.get_login_info()
        await client.close()
        assert session.close_calls == 1 and session.closed
        await client.close()
        assert session.close_calls == 1, "重复关闭不得重复关闭会话"

    asyncio.run(scenario())


def test_关闭预算内未完成如实报告且第二次立即返回() -> None:

    async def scenario() -> None:
        session = HangingSession(lambda url, body, headers, kwargs: MemoryResponse(200))
        clock = FakeClock()
        client, _ = memory_transport(session, clock=clock)
        await client.get_login_info()
        closer = asyncio.create_task(client.close())
        await spin_until(lambda: session.close_calls == 1)
        clock.advance(transport.ONEBOT_CLOSE_TIMEOUT_SECONDS + 1.0)
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await closer
        assert caught.value.category == "close_incomplete"
        assert await asyncio.wait_for(client.close(), timeout=1.0) is None, "第二次关闭必须立即返回"

    asyncio.run(scenario())


def test_关闭取消在途请求且之后不再提交() -> None:
    async def scenario() -> None:
        session = MemorySession(lambda url, body, headers, kwargs: MemoryResponse(200, hang=True))
        client, _ = memory_transport(session)
        call = asyncio.create_task(client.get_login_info())
        await spin_until(lambda: len(session.posts) == 1)
        await client.close()
        with pytest.raises(transport.OneBotUnavailable) as caught:
            await call
        assert caught.value.category == "cancelled" and caught.value.submitted
        with pytest.raises(transport.OneBotUnavailable) as after:
            await client.get_login_info()
        assert after.value.category == "closed" and not after.value.submitted
        assert len(session.posts) == 1, "关闭后不得发出新请求"

    asyncio.run(scenario())


def test_公开动作面只有固定七动作与关闭() -> None:

    public = {name for name in dir(transport.AiohttpOneBotTransport) if not name.startswith("_")}
    assert {"get_login_info", "get_group_list", "get_msg", "get_forward_msg", "send_msg",
            "send_group_forward_msg", "send_forward_msg", "close"} <= public
    assert "forward_group_single_msg" not in public, "旧卡片按标识重建动作已移除，不得残留"
    assert not any(name in public for name in (
        "delete_msg", "send_group_msg", "send_private_msg", "upload_forward_msg", "get_status", "set_group_ban",
    ))


def test_隔离子进程无SDK与网络库导入且构造不联网() -> None:
    program = '''
import importlib, sys, types
pkg = types.ModuleType("isolated_courier")
pkg.__path__ = [sys.argv[1]]
sys.modules[pkg.__name__] = pkg
m = importlib.import_module("isolated_courier.snowluma_transport")
t = m.AiohttpOneBotTransport(m.OneBotConnection("http://127.0.0.1:3000", "FAKE_TOKEN"), environ={})
assert "maibot_sdk" not in sys.modules
assert "aiohttp" not in sys.modules
assert not any(n.startswith(("service", "history")) for n in sys.modules)
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(Path(__file__).resolve().parents[1])],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
