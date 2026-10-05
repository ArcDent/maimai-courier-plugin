"""离线验收：公共 Hook/Tool 经真实 aiohttp 回环 **HTTP** 端点与本地合成 OneBot 互证。

除以太替身（`aiohttp.web` 回环服务端）外全程真实：插件经公开工厂装配（真实
`AiohttpOneBotTransport`、注入时钟与真实 `open_ledger`），宿主上下文用 fake SDK
替身，账本用真实 SQLite 临时目录账本。回环服务端只按冻结协议返回合成信封；实际
请求、占位时序与落账全部从公共边界（工具结果、账本记录、请求日志）断言，不访问
任何客户端私有状态。端点只绑 `127.0.0.1`；每个用例自建临时目录、端点与账本，
用例间无共享状态。默认 pytest 档（无 `aiohttp`）在导入期跳过本文件。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web

from maimai_courier_plugin.shared.delivery import LedgerUnavailable
from maimai_courier_plugin.delivery_ledger import LEDGER_FILE_NAME, open_ledger
from maimai_courier_plugin.snowluma_transport import (
    ONEBOT_ACTION_TIMEOUT_SECONDS,
    AiohttpOneBotTransport,
    OneBotConnection,
)
from tests.direct_support import (
    ACCEPTANCE_TOKEN,
    DEFAULT_MEDIA_URL,
    DEFAULT_RESOURCE_ID,
    FaultLedger,
    at_segment,
    forward_node,
    forward_segment,
    image_segment,
    merged_forward_message,
    onebot_segments,
    prepare_source,
)
from tests.snowluma_transport_support import http_endpoint
from tests.support import (
    MESSAGE_ID,
    OTHER_TARGET_GROUP_ID,
    SOURCE_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    FakeClock,
    enabled_config,
    envelope,
    ledger_key,
    make_plugin,
    open_test_ledger,
    spin_until,
    text_segment,
)

SEND_ACTIONS = frozenset({"send_msg", "send_group_forward_msg", "send_forward_msg"})
"""固定发送动作表：卡片展开节点重组。"""

SECRET_TEXTS = ("不可回显的合成源正文", "不可回显的合成评论", ACCEPTANCE_TOKEN)
"""结果与日志都不得出现的合成敏感内容（源正文、评论正文与令牌）。"""

WIRE_RESOURCE_ID = "wire-res-id"
"""卡片重组投递信封额外返回的资源标识：不得作为成功证据、不入账、不进结果。"""

WIRE_FORWARD_ID = "wire-forward-id"
"""卡片重组投递信封额外返回的合并转发标识（同上，只作平台内部资源引用）。"""

NODE_SENDER = {"user_id": "90001", "nickname": "群友", "card": ""}
"""平台源记录的发送者署名：文字单节点卡片的 `uin`/`name` 只来自这里。"""

EXPANDED_MESSAGES: list[dict[str, Any]] = [
    {"sender": {"user_id": "90001", "nickname": "群友", "card": ""},
     "message": [{"type": "text", "data": {"text": "合成展开节点一"}}]},
    {"sender": {"user_id": "90002", "card": "名片乙"},
     "message": [{"type": "image", "data": {"url": DEFAULT_MEDIA_URL}},
                 {"type": "forward", "data": {"id": "old-res-id", "content": [
                     {"type": "text", "data": {"text": "内层卡片"}}]}}]},
]
"""`get_forward_msg` 展开查询的合成应答：第二节点内容含嵌套 forward 段（原样提交）。"""

MATERIALS = [
    pytest.param(
        [text_segment("甲\n乙")],
        {"action": "send_forward_msg", "params": {"group_id": TARGET_GROUP_ID, "messages": [
            {"type": "node", "data": {"uin": "90001", "name": "群友",
                                      "content": [{"type": "text", "data": {"text": "甲\n乙"}}]}}]}},
        id="文字",
    ),
    pytest.param(
        [image_segment()],
        {"action": "send_msg", "params": {
            "message_type": "group", "group_id": TARGET_GROUP_ID,
            "message": [{"type": "image", "data": {"url": DEFAULT_MEDIA_URL}}]}},
        id="单图",
    ),
    pytest.param(
        [image_segment(), image_segment()],
        {"action": "send_msg", "params": {
            "message_type": "group", "group_id": TARGET_GROUP_ID,
            "message": [{"type": "image", "data": {"url": DEFAULT_MEDIA_URL}},
                        {"type": "image", "data": {"url": DEFAULT_MEDIA_URL.replace(".png", "-2.png")}}]}},
        id="多图",
    ),
    pytest.param(
        [text_segment("前"), image_segment(), text_segment("后")],
        {"action": "send_msg", "params": {
            "message_type": "group", "group_id": TARGET_GROUP_ID,
            "message": [{"type": "text", "data": {"text": "前"}},
                        {"type": "image", "data": {"url": DEFAULT_MEDIA_URL}},
                        {"type": "text", "data": {"text": "后"}}]}},
        id="图文",
    ),
    pytest.param(
        [text_segment("甲"), at_segment("30001", nickname="乙"), at_segment("30002"), at_segment("all")],
        {"action": "send_forward_msg", "params": {"group_id": TARGET_GROUP_ID, "messages": [
            {"type": "node", "data": {"uin": "90001", "name": "群友", "content": [
                {"type": "text", "data": {"text": "甲"}},
                {"type": "at", "data": {"qq": "30001", "nickname": "乙"}},
                {"type": "at", "data": {"qq": "30002"}},
                {"type": "at", "data": {"qq": "all"}}]}}]}},
        id="提及",
    ),
    pytest.param(
        [forward_segment(forward_node(content=[
            at_segment("all"), {"type": "reply", "data": "内部回复"}, {"type": "unknown", "data": "内部未知"}]))],
        {"action": "send_group_forward_msg", "params": {"group_id": TARGET_GROUP_ID, "messages": [
            {"type": "node", "data": {"uin": "90001", "name": "群友",
                                      "content": [{"type": "text", "data": {"text": "合成展开节点一"}}]}},
            {"type": "node", "data": {"uin": "90002", "name": "名片乙", "content": [
                {"type": "image", "data": {"url": DEFAULT_MEDIA_URL}},
                {"type": "forward", "data": {"id": "old-res-id", "content": [
                    {"type": "text", "data": {"text": "内层卡片"}}]}}]}}]}},
        id="原卡片",
    ),
]
"""各候选类型的期望发送线路。卡片行：源卡片只提供卡片标识给只读
`get_forward_msg` 展开查询，发送载荷是展开应答构造的节点数组（含嵌套 forward 段
原样），不是源消息内部内容，也不含源卡片资源 ID。"""


def comment_wire(comment: str) -> dict[str, Any]:

    return {"action": "send_msg", "params": {
        "message_type": "group", "group_id": TARGET_GROUP_ID,
        "message": [{"type": "text", "data": {"text": comment}}]}}


async def bound_call(plugin: Any, *, comment: Any = None, target: str = TARGET_GROUP_ID,
                     message_id: str = MESSAGE_ID) -> dict[str, Any]:

    arguments: dict[str, Any] = {"message_id": message_id, "target_group_id": target}
    if comment is not None:
        arguments["comment"] = comment
    bound = await plugin.bind_forward_source(
        session_id=SOURCE_SESSION_ID,
        output_items=[{
            "item_type": "FunctionCallItem",
            "meta": {"item_id": "item-1"},
            "tool_call": {"call_id": "call-1", "func_name": "forward_message",
                          "args": arguments, "extra_content": None},
        }],
        item_schema_version=1,
    )
    return await plugin.forward_message(**bound["modified_kwargs"]["output_items"][0]["tool_call"]["args"])


class LocalOneBot:
    """只模拟冻结的协议响应；实际请求、占位与落账均从公共边界记录。"""

    def __init__(self, clock: FakeClock, raw: list[dict[str, Any]], *, fault: str = "",
                 ledger: Any = None, account: str = SOURCE_ACCOUNT_ID,
                 targets: tuple[str, ...] = (TARGET_GROUP_ID, OTHER_TARGET_GROUP_ID),
                 token: str = ACCEPTANCE_TOKEN, negative_ids: bool = False) -> None:
        self.clock = clock
        self.raw = raw
        self.fault = fault
        self.ledger = ledger
        self.account = account
        self.targets = list(targets)
        self.token = token
        self.negative_ids = negative_ids
        self.requests: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.observations: list[tuple[str, str, float]] = []
        self.login_count = 0
        self.pause_login = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def source(self) -> dict[str, Any]:

        return {"message_id": MESSAGE_ID, "message_type": "group", "group_id": SOURCE_GROUP_ID,
                "user_id": NODE_SENDER["user_id"], "message": onebot_segments(self.raw),
                "sender": dict(NODE_SENDER)}

    async def handler(self, request: web.Request) -> web.Response:
        assert request.headers.get("Authorization") == f"Bearer {self.token}", "令牌只经认证头"
        body = await request.json()
        self.requests.append({"method": request.method, "path": request.path,
                              "query": request.query_string, "body": body,
                              "authorization": request.headers.get("Authorization")})
        action, params = body["action"], body["params"]
        if action == "get_login_info":
            self.login_count += 1
            if self.login_count == self.pause_login:
                self.entered.set()
                await self.release.wait()
                if self.fault == "login_unavailable":
                    # 释放后连接中断：查询不可用必须按可定位失败处理，不得继续发送。
                    request.transport.abort()
                    return web.Response(status=599)
            data: Any = ({"user_id": "wrong-account"} if self.fault == "wrong_account"
                         else {"user_id": self.account})
        elif action == "get_group_list":
            data = [{"group_id": value} for value in self.targets]
        elif action == "get_msg":
            data = self._record_for(params["message_id"])
        elif action == "get_forward_msg":
            # 只读展开查询：按源卡片标识返回合成节点序列。
            data = {"messages": deepcopy(EXPANDED_MESSAGES)}
        elif action in SEND_ACTIONS:
            return await self._send(request, action, params)
        else:
            raise AssertionError(f"非约定动作：{action}")
        return web.json_response(envelope(data))

    def _record_for(self, message_id: str) -> Any:
        if message_id == MESSAGE_ID:
            return self.source
        if message_id in self.receipts:
            record = deepcopy(self.receipts[message_id])
            if self.fault == "wrong_record":
                record["message_id"] = "another-record"
            elif self.fault == "wrong_target":
                record["group_id"] = "99999"
            return record
        return None

    async def _send(self, request: web.Request, action: str, params: dict[str, Any]) -> web.Response:
        self.sent.append({"action": action, "params": params})
        if self.ledger is not None:
            row = self.ledger.get(ledger_key(target_group_id=params["group_id"]))
            self.observations.append((row.status, row.comment_status, self.clock.now()))
        message_id = f"-wire-target-{len(self.sent)}" if self.negative_ids else f"wire-target-{len(self.sent)}"
        self.receipts[message_id] = {
            "message_id": message_id, "message_type": "group", "group_id": params["group_id"],
            "user_id": self.account, "message_seq": len(self.sent),
        }
        if self.fault == "after_send":
            # 平台已受理但连接中断：客户端必须保持待核实，不得重发。
            request.transport.abort()
            return web.Response(status=599)
        if self.fault == "no_response":
            self.entered.set()
            await self.release.wait()
        if self.fault == "business_failed":
            return web.json_response(envelope(None, status="failed", retcode=100,
                                              message="目标不存在"))
        if self.fault == "no_id":
            data: Any = {}
        else:
            data = {"message_id": message_id}
            # 发送动作额外返回的资源标识：只作信封字段，不得作为成功证据。
            data["res_id"] = WIRE_RESOURCE_ID
            data["forward_id"] = WIRE_FORWARD_ID
        return web.json_response(envelope(data))


@dataclasses.dataclass
class Assembly:
    """一次公开装配的句柄：插件、宿主上下文、适配器 API 侦听与自建真实传输。"""

    plugin: Any
    ctx: Any
    api_calls: list[Any]
    transports: list[Any]


def assemble(path: Path, url: str, raw: list[dict[str, Any]], clock: FakeClock, *,
             ledger: Any = None, token_env_var: str = "ACCEPTANCE_TOKEN",
             targets: tuple[str, ...] = (TARGET_GROUP_ID, OTHER_TARGET_GROUP_ID),
             message_types: tuple[str, ...] = ("forward", "text", "image")
             ) -> Assembly:

    config = enabled_config(target_groups=targets, message_types=message_types)
    config["snowluma"] = {"http_url": url, "token_env_var": token_env_var}
    environ = {token_env_var: ACCEPTANCE_TOKEN}
    transports: list[Any] = []

    def factory(connection: Any) -> Any:
        transport = AiohttpOneBotTransport(connection, clock=clock, environ=environ)
        transports.append(transport)
        return transport

    plugin, ctx = make_plugin(path, clock=clock, config=config, environ=environ,
                              transport_factory=factory,
                              ledger_opener=None if ledger is None else lambda _: ledger)
    prepare_source(ctx, raw_message=raw)
    api_calls: list[Any] = []

    async def forbidden_api(*args: Any, **kwargs: Any) -> None:
        api_calls.append((args, kwargs))
        raise AssertionError("不得借适配器 API 发送")

    ctx.api = SimpleNamespace(call=forbidden_api)
    return Assembly(plugin=plugin, ctx=ctx, api_calls=api_calls, transports=transports)


async def close_assembly(assembly: Assembly, *ledgers: Any, unload: bool = True) -> None:

    if unload:
        await assembly.plugin.on_unload()
    await close_transports(assembly.transports)
    for ledger in ledgers:
        ledger.close()


async def close_transports(transports: list[Any]) -> None:

    for transport in transports:
        await transport.close()


async def assert_restart_no_resend(
    ledger: Any, *, tmp_path: Path, url: str, raw: list[dict[str, Any]], clock: FakeClock,
    server: LocalOneBot, expected_status: str, comment: str,
    expected_comment_status: str | None = None,
) -> None:

    before = len(server.sent)
    second = assemble(tmp_path, url, raw, clock, ledger=ledger)
    try:
        repeated = await bound_call(second.plugin, comment=comment)
        assert repeated["status"] == expected_status
        if expected_comment_status is not None:
            assert repeated["comment_status"] == expected_comment_status
        assert len(server.sent) == before, "重启后不得自动补发源或评论"
        assert second.ctx.send.calls == [] and second.api_calls == []
    finally:
        await close_assembly(second)


def assert_clean_requests(server: LocalOneBot) -> None:

    assert server.requests, "验收必须经真实 HTTP 端点产生请求"
    for item in server.requests:
        assert item["method"] == "POST"
        assert item["query"] == ""
        assert item["authorization"] == f"Bearer {server.token}"
        assert server.token not in item["path"] + item["query"], "令牌不得进入 URL"


def assert_no_leak(result: dict[str, Any], ctx: Any, secrets: tuple[str, ...] = SECRET_TEXTS) -> None:

    observed = json.dumps([result, ctx.logger.records], ensure_ascii=False)
    for secret in secrets:
        assert secret not in observed, secret


# --------------------------------------------------------------------------- 主互证


@pytest.mark.parametrize("raw,expected", MATERIALS)
@pytest.mark.parametrize("comment", ["", " \n\t", "[CQ:at,qq=all] 本次评论"], ids=["空", "空白", "非空"])
def test_公共工具经真实HTTP按类型分派完整材料评论与冷目标(raw, expected, comment, tmp_path):
    async def scenario():
        clock = FakeClock()
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(clock, raw, ledger=ledger)
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                await assembly.plugin.on_load()
                assert server.requests == []
                task = asyncio.create_task(bound_call(assembly.plugin, comment=comment))
                if comment.strip():
                    await spin_until(lambda: 0.5 in clock.sleeps)
                    assert len(server.sent) == 1 and not task.done()
                    assert ledger.get(ledger_key()).status == "sent"
                    clock.advance(0.49)
                    for _ in range(20):
                        await asyncio.sleep(0)
                    assert len(server.sent) == 1, "0.5 秒固定延迟未满不得提交评论"
                    clock.advance(0.01)
                else:
                    assert 0.5 not in clock.sleeps, "空白评论不等待、不占位、不提交授权"
                result = await asyncio.wait_for(task, 5)
                expected_sends = [expected] + ([comment_wire(comment)] if comment.strip() else [])
                assert server.sent == expected_sends, "三类 kind 与评论按固定动作分派"
                assert result["status"] == "sent" and result["success"] and result["delivered"]
                assert result["target_message_id"] == "wire-target-1"
                row = ledger.get(ledger_key())
                assert (row.status, row.target_message_id) == ("sent", "wire-target-1")
                assert (row.comment_status, row.comment_message_id) == (
                    ("sent", "wire-target-2") if comment.strip() else ("", ""))
                assert (result["comment_status"], result["comment_message_id"]) == (
                    row.comment_status, row.comment_message_id)
                assert server.observations == (
                    [("sending", "", 0.0), ("sent", "sending", 0.5)] if comment.strip()
                    else [("sending", "", 0.0)]), "发送前必须已持久占位"
                assert_clean_requests(server)
                assert DEFAULT_RESOURCE_ID not in json.dumps(server.sent, ensure_ascii=False), (
                    "源卡片资源 ID 不得进入发送载荷（展开查询按标识读取，发送只交节点数组）")
                assert WIRE_RESOURCE_ID not in json.dumps(result, ensure_ascii=False), (
                    "卡片发送信封额外返回的资源标识不得进入工具结果")
                assert WIRE_RESOURCE_ID not in json.dumps(assembly.ctx.logger.records, ensure_ascii=False), (
                    "卡片发送信封额外返回的资源标识不得进入日志")
                assert WIRE_RESOURCE_ID not in repr(row), "资源标识不得写入账本"
                if any(item["body"]["action"] == "send_group_forward_msg" for item in server.requests):
                    assert [item["body"]["params"] for item in server.requests
                            if item["body"]["action"] == "get_forward_msg"] == [
                        {"id": DEFAULT_RESOURCE_ID}], "卡片路径先按源卡片标识展开查询"
                assert assembly.ctx.maisaka.proactive.calls == []
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
                assert len(assembly.ctx.chat.streams) == 1, "目标没有宿主活动会话也可发送（不保证历史同步）"
                assert_no_leak(result, assembly.ctx)
            finally:
                await close_assembly(assembly, ledger)

    asyncio.run(scenario())


def test_负数目标消息ID经真实HTTP按原值核验落账(tmp_path):

    async def scenario():
        clock = FakeClock()
        raw = [forward_segment(forward_node())]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(clock, raw, ledger=ledger, negative_ids=True)
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                result = await bound_call(assembly.plugin)
                assert result["status"] == "sent", result
                assert result["target_message_id"] == "-wire-target-1"
                assert [item["body"]["params"]["message_id"] for item in server.requests
                        if item["body"]["action"] == "get_msg"] == [MESSAGE_ID, "-wire-target-1"], (
                    "回查只针对本次返回的目标标识，负号不改写")
                row = ledger.get(ledger_key())
                assert (row.status, row.target_message_id) == ("sent", "-wire-target-1")
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
            finally:
                await close_assembly(assembly, ledger)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 故障互证


@pytest.mark.parametrize("fault,status,source_state,comment_state,send_count,stage", [
    pytest.param("login_unavailable", "failed", "failed", "", 0, "source_send", id="登录查询断连"),
    pytest.param("after_send", "unknown", "unknown", "", 1, "source_send", id="受理后断连"),
    pytest.param("no_response", "unknown", "unknown", "", 1, "source_send", id="应答挂起超时"),
    pytest.param("no_id", "unknown", "unknown", "", 1, "source_send", id="缺目标ID"),
    pytest.param("business_failed", "failed", "failed", "", 1, "source_send", id="业务失败"),
    pytest.param("wrong_account", "rejected", None, "", 0, "rejected", id="账号不符"),
    pytest.param("wrong_record", "unknown", "unknown", "", 1, "source_send", id="回查记录不符"),
    pytest.param("wrong_target", "unknown", "unknown", "", 1, "source_send", id="回查目标不符"),
    pytest.param("reserve", "ledger_unavailable", None, "", 0, "source_persist", id="源占位失败"),
    pytest.param("mark_sent", "unknown", "sending", "", 1, "source_persist", id="源落账失败"),
    pytest.param("reserve_comment", "partial", "sent", "", 1, "comment_persist", id="评论占位失败"),
    pytest.param("record_comment", "partial", "sent", "sending", 2, "comment_persist", id="评论落账失败"),
])
def test_真实HTTP故障互证工具账本线路且重启不补发(tmp_path, fault, status, source_state,
                                                  comment_state, send_count, stage):
    async def scenario():
        clock = FakeClock()
        raw = [text_segment("不可回显的合成源正文")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        storage = FaultLedger(ledger)
        storage.fail_next(fault)
        server = LocalOneBot(clock, raw, fault=fault, ledger=ledger)
        if fault == "login_unavailable":
            server.pause_login = 2
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=storage)
            try:
                task = asyncio.create_task(bound_call(assembly.plugin, comment="不可回显的合成评论"))
                if fault == "login_unavailable":
                    await asyncio.wait_for(server.entered.wait(), 5)
                    server.release.set()
                elif fault == "no_response":
                    await asyncio.wait_for(server.entered.wait(), 5)
                    clock.advance(ONEBOT_ACTION_TIMEOUT_SECONDS)
                elif fault in ("reserve_comment", "record_comment"):
                    await spin_until(lambda: 0.5 in clock.sleeps)
                    clock.advance(0.5)
                result = await asyncio.wait_for(task, 5)
                assert (result["status"], result["stage"], result["success"]) == (status, stage, False)
                assert len(server.sent) == send_count
                row = ledger.get(ledger_key())
                assert (None if row is None else row.status) == source_state
                if row is not None:
                    assert row.comment_status == comment_state
                if fault in ("mark_sent", "reserve_comment", "record_comment"):
                    assert result["sent"] and result["target_message_id"] == "wire-target-1"
                if fault == "record_comment":
                    assert (result["comment_status"], result["comment_message_id"]) == (
                        "unknown", "wire-target-2")
                if fault == "business_failed":
                    assert [item["body"]["params"]["message_id"] for item in server.requests
                            if item["body"]["action"] == "get_msg"] == [MESSAGE_ID], (
                        "业务失败按明确结果处理，不再回查目标")
                assert_no_leak(result, assembly.ctx)
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
            finally:
                server.release.set()
                await close_assembly(assembly, ledger)
            reopened = open_test_ledger(tmp_path, clock=clock)
            try:
                row = reopened.get(ledger_key())
                assert (None if row is None else row.status) == (
                    "unknown" if source_state == "sending" else source_state)
                if row is not None:
                    assert row.comment_status == (
                        "unknown" if comment_state == "sending" else comment_state)
                if source_state not in (None, "failed"):
                    await assert_restart_no_resend(
                        reopened, tmp_path=tmp_path, url=url, raw=raw, clock=clock,
                        server=server, comment="换评论也不补发",
                        expected_status="already_forwarded" if source_state == "sent" else "unknown")
            finally:
                reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["关闭", "撤销目标", "切换连接", "卸载"])
def test_真实HTTP评论等待撤权保留本次结果与持久字段边界(tmp_path, change):
    async def scenario():
        clock, raw = FakeClock(), [text_segment("源")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(clock, raw, ledger=ledger)
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                task = asyncio.create_task(bound_call(assembly.plugin, comment="停止的评论"))
                await spin_until(lambda: 0.5 in clock.sleeps)
                if change == "卸载":
                    await assembly.plugin.on_unload()
                else:
                    config = assembly.plugin.config.model_dump()
                    if change == "关闭":
                        config["plugin"]["enabled"] = False
                    elif change == "撤销目标":
                        config["scope"]["target_groups"] = []
                    else:
                        config["snowluma"]["token_env_var"] = "OTHER_TOKEN_REFERENCE"
                    assembly.plugin.set_plugin_config(config)
                    await assembly.plugin.on_config_update("self", config, "test")
                    clock.advance(0.5)
                result = await asyncio.wait_for(task, 5)
                assert (result["status"], result["comment_status"], result["target_message_id"]) == (
                    "partial", "failed", "wire-target-1")
                assert len(server.sent) == 1, "撤权后不得提交评论发送"
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
            finally:
                await close_assembly(assembly, ledger, unload=change != "卸载")
            reopened = open_test_ledger(tmp_path, clock=clock)
            try:
                row = reopened.get(ledger_key())
                assert (row.status, row.comment_status, row.comment_message_id) == ("sent", "", "")
                await assert_restart_no_resend(
                    reopened, tmp_path=tmp_path, url=url, raw=raw, clock=clock,
                    server=server, comment="不会补发", expected_status="already_forwarded",
                    expected_comment_status="")
            finally:
                reopened.close()

    asyncio.run(scenario())


def test_真实HTTP多客户端并存关闭隔离且另一目标独立(tmp_path):
    async def scenario():
        clock, raw = FakeClock(), [text_segment("仅指定目标")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(clock, raw, ledger=ledger)
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=ledger)
            peer = AiohttpOneBotTransport(
                OneBotConnection(url, "ACCEPTANCE_TOKEN", ACCEPTANCE_TOKEN), clock=clock,
                environ={"ACCEPTANCE_TOKEN": ACCEPTANCE_TOKEN})
            try:
                first, peer_reply = await asyncio.gather(
                    bound_call(assembly.plugin), peer.get_login_info())
                assert first["target_message_id"] == "wire-target-1"
                assert peer_reply["data"]["user_id"] == SOURCE_ACCOUNT_ID
                second = await bound_call(assembly.plugin, target=OTHER_TARGET_GROUP_ID)
                assert second["target_message_id"] == "wire-target-2"
                assert [item["params"]["group_id"] for item in server.sent] == [
                    TARGET_GROUP_ID, OTHER_TARGET_GROUP_ID]
                assert ledger.get(ledger_key()).target_message_id == "wire-target-1"
                assert ledger.get(ledger_key(target_group_id=OTHER_TARGET_GROUP_ID)).target_message_id == (
                    "wire-target-2")
                assert ledger.get(ledger_key()).comment_status == ""
                assert 0.5 not in clock.sleeps, "评论参数缺省不等待也不占位"
                await assembly.plugin.on_unload()
                assert (await peer.get_login_info())["data"]["user_id"] == SOURCE_ACCOUNT_ID, (
                    "卸载只结束本插件自己的在途请求，不影响同一端点的其他客户端")
                assert len(server.sent) == 2
                assert assembly.ctx.send.calls == []
                assert assembly.ctx.maisaka.proactive.calls == [] and assembly.api_calls == []
            finally:
                await close_assembly(assembly, unload=False)
                await peer.close()
                ledger.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("history_name", ["service_binding.json", "service_binding.json.tmp",
                                          "old-ledger.snapshot"])
def test_真实端点可用但服务历史痕迹使公共工具闭锁(tmp_path, history_name):
    async def scenario():
        evidence = tmp_path / history_name
        evidence.write_bytes(b"synthetic-history-not-new-install")
        clock, raw = FakeClock(), [text_segment("不发送")]
        server = LocalOneBot(clock, raw)
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock)
            try:
                result = await bound_call(assembly.plugin)
                assert result["status"] == "ledger_unavailable" and result["sent"] is False
                assert server.requests == [] and server.sent == []
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
                assert evidence.read_bytes() == b"synthetic-history-not-new-install"
                assert not (tmp_path / LEDGER_FILE_NAME).exists()
                assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable)
            finally:
                await close_assembly(assembly)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["关闭", "撤销目标", "卸载"])
def test_真实HTTP源账号查询等待后复验权限(tmp_path, change):
    async def scenario():
        clock, raw = FakeClock(), [text_segment("未提交源")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(clock, raw, ledger=ledger)
        server.pause_login = 2
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                task = asyncio.create_task(bound_call(assembly.plugin, comment="不应发送"))
                await asyncio.wait_for(server.entered.wait(), 5)
                if change == "卸载":
                    # 先启动卸载使许可失效，释放模拟服务端以允许有限关闭。
                    stop = asyncio.create_task(assembly.plugin.on_unload())
                    await asyncio.sleep(0)
                    server.release.set()
                    await stop
                else:
                    config = assembly.plugin.config.model_dump()
                    if change == "关闭":
                        config["plugin"]["enabled"] = False
                    else:
                        config["scope"]["target_groups"] = []
                    assembly.plugin.set_plugin_config(config)
                    await assembly.plugin.on_config_update("self", config, "test")
                    server.release.set()
                result = await asyncio.wait_for(task, 5)
                assert (result["status"], result["stage"], result["sent"]) == ("rejected", "rejected", False), (
                    "复验未通过按明确未发送拒绝")
                assert server.sent == []
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
            finally:
                server.release.set()
                await close_assembly(assembly, ledger)
            reopened = open_test_ledger(tmp_path, clock=clock)
            try:
                assert reopened.get(ledger_key()).status == "failed"
            finally:
                reopened.close()

    asyncio.run(scenario())


def test_宿主Hook与真实端点并存只有源群判断且沉默不发送(tmp_path):

    async def scenario():
        clock, raw = FakeClock(), [forward_segment(forward_node())]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(clock, raw, ledger=ledger)
        async with http_endpoint(server.handler) as url:
            assembly = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                assert (await bound_call(assembly.plugin))["status"] == "sent"
                before = len(server.requests)
                payload = merged_forward_message(message_id=MESSAGE_ID, raw_message=raw)
                await asyncio.gather(assembly.plugin.observe_received_message(**payload),
                                     assembly.plugin.observe_received_message(**payload))
                await spin_until(lambda: len(assembly.ctx.maisaka.proactive.calls) == 1)
                for _ in range(100):
                    await asyncio.sleep(0)
                assert len(assembly.ctx.maisaka.proactive.calls) == 1
                assert assembly.ctx.maisaka.proactive.calls[0]["stream_id"] == SOURCE_SESSION_ID
                assert len(server.requests) == before, "沉默的判断不得触发新的平台请求"
                assert len(server.sent) == 1
                assert ledger.get(ledger_key()).status == "sent"
                assert assembly.ctx.send.calls == [] and assembly.api_calls == []
            finally:
                await close_assembly(assembly, ledger)

    asyncio.run(scenario())
