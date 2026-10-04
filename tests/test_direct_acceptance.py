"""第8组离线验收：公共 Hook/Tool 经真实 aiohttp 连接本地合成 OneBot。"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web

from delivery_ledger import LEDGER_FILE_NAME, LedgerUnavailable, open_ledger
from maimai_courier_plugin.snowluma_transport import AiohttpOneBotTransport, OneBotConnection
from tests.direct_support import FaultLedger, onebot_segments
from tests.snowluma_transport_support import endpoint, reply, spin_until
from tests.support import (FakeClock, SOURCE_ACCOUNT_ID, SOURCE_GROUP_ID, SOURCE_SESSION_ID,
    TARGET_GROUP_ID, at_segment, enabled_config, forward_node, forward_segment,
    function_call_item, image_segment, ledger_key, make_plugin, open_test_ledger,
    prepare_source, text_segment)


async def bound_call(plugin, *, comment=None, target=TARGET_GROUP_ID):
    """走宿主公开来源 Hook，再把其产出的参数交给公开工具；None表示省略评论参数。"""
    arguments = {"message_id": "m-source-1", "target_group_id": target}
    if comment is not None:
        arguments["comment"] = comment
    items = [function_call_item(func_name="forward_message", arguments=arguments)]
    bound = await plugin.bind_forward_source(session_id=SOURCE_SESSION_ID, output_items=items,
                                            item_schema_version=1)
    return await plugin.forward_message(**bound["modified_kwargs"]["output_items"][0]["tool_call"]["args"])


class LocalOneBot:
    """只模拟冻结的协议响应；实际请求、占位和时间均从公共边界记录。"""
    def __init__(self, raw, clock, *, fault="", ledger=None):
        self.raw, self.clock, self.fault, self.ledger = raw, clock, fault, ledger
        self.frames, self.sends, self.sockets, self.observations = [], [], [], []
        self.records = {}
        self.pause_login = 0
        self.login_count = 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.http_calls = []

    async def handler(self, request):
        if request.headers.get("Upgrade", "").lower() != "websocket":
            self.http_calls.append((request.method, request.path))
            return web.Response(status=410)
        assert request.headers.get("Authorization") == "Bearer synthetic-acceptance-only"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            async for frame in ws:
                body = json.loads(frame.data)
                self.frames.append(body)
                action, params = body["action"], body["params"]
                # 同 echo 的伪装事件与别的客户端响应均不能完成当前动作。
                await ws.send_json({"post_type": "message", "echo": body["echo"],
                                    "status": "ok", "data": {"message_id": "event-not-receipt"}})
                await ws.send_json({"status": "ok", "echo": "other-client-old-echo", "data": {}})
                if action == "get_login_info":
                    self.login_count += 1
                    if self.login_count == self.pause_login:
                        self.entered.set()
                        await self.release.wait()
                    if self.fault == "before_send" and self.login_count == 2:
                        await ws.close()
                        break
                    data = {"user_id": "wrong-account" if self.fault == "wrong_account" else SOURCE_ACCOUNT_ID}
                elif action == "get_group_list":
                    data = [{"group_id": TARGET_GROUP_ID}, {"group_id": "30003"}]
                elif action == "get_msg":
                    mid = params["message_id"]
                    if mid == "m-source-1":
                        data = {"message_id": mid, "message_type": "group", "group_id": SOURCE_GROUP_ID,
                                "message": onebot_segments(self.raw)}
                    else:
                        data = deepcopy(self.records[mid])
                        if self.fault == "wrong_record":
                            data["message_id"] = "another-record"
                        elif self.fault == "wrong_target":
                            data["group_id"] = "99999"
                elif action == "send_msg":
                    self.sends.append(body)
                    if self.ledger is not None:
                        row = self.ledger.get(ledger_key(target_group_id=params["group_id"]))
                        self.observations.append((row.status, row.comment_status, self.clock.now()))
                    mid = f"wire-target-{len(self.sends)}"
                    self.records[mid] = {"message_id": mid, "message_type": "group", "group_id": params["group_id"],
                                         "user_id": SOURCE_ACCOUNT_ID, "message_seq": len(self.sends)}
                    if self.fault == "after_send":
                        await ws.close()
                        break
                    if self.fault == "no_response":
                        self.entered.set()
                        continue
                    data = {} if self.fault == "no_id" else {"message_id": mid}
                else:
                    raise AssertionError(f"非约定动作：{action}")
                # 广播真实动作响应，模拟服务端不提供请求连接独收的最坏情况。
                for connection in self.sockets:
                    if not connection.closed:
                        await reply(connection, body, data)
        finally:
            await ws.close()
        return ws


def assemble(path, url, raw, clock, *, ledger=None):
    config = enabled_config(target_groups=(TARGET_GROUP_ID, "30003"))
    config["snowluma"] = {"websocket_url": url, "token_env_var": "ACCEPTANCE_TOKEN"}
    plugin, ctx = make_plugin(path, clock=clock, config=config,
        environ={"ACCEPTANCE_TOKEN": "synthetic-acceptance-only"},
        ledger_opener=None if ledger is None else lambda _: ledger)
    prepare_source(ctx, raw_message=raw)
    api_calls = []
    async def forbidden_api(*args, **kwargs):
        api_calls.append((args, kwargs))
        raise AssertionError("不得借适配器 API 发送")
    ctx.api = SimpleNamespace(call=forbidden_api)
    return plugin, ctx, api_calls


MATERIALS = [
    pytest.param([text_segment("甲\n乙")], [{"type": "text", "data": {"text": "甲\n乙"}}], id="文字"),
    pytest.param([image_segment()], [{"type": "image", "data": {"url": "https://cdn.example.invalid/media.png"}}], id="单图"),
    pytest.param([image_segment(), image_segment()], [
        {"type": "image", "data": {"url": "https://cdn.example.invalid/media.png"}},
        {"type": "image", "data": {"url": "https://cdn.example.invalid/media-2.png"}}], id="多图"),
    pytest.param([text_segment("前"), image_segment(), text_segment("后")], [
        {"type": "text", "data": {"text": "前"}},
        {"type": "image", "data": {"url": "https://cdn.example.invalid/media.png"}},
        {"type": "text", "data": {"text": "后"}}], id="图文"),
    pytest.param([text_segment("甲"), at_segment("30001", nickname="乙"), at_segment("30002"), at_segment("all")],
        [{"type": "text", "data": {"text": "甲［提及：乙（30001）］［提及：30002］［提及：全体成员］"}}], id="提及"),
    pytest.param([forward_segment(forward_node(content=[at_segment("all"), {"type": "reply", "data": "内部回复"},
        {"type": "unknown", "data": "内部未知"}]))],
        [{"type": "forward", "data": {"id": "res-card-synthetic"}}], id="原卡片"),
]


@pytest.mark.parametrize("raw,expected", MATERIALS)
@pytest.mark.parametrize("comment", ["", " \n\t", "[CQ:at,qq=all] 本次评论"], ids=["空", "空白", "非空"])
def test_公共工具经真实WS完整材料评论与冷目标(raw, expected, comment, tmp_path):
    async def scenario():
        clock = FakeClock()
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(raw, clock, ledger=ledger)
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                await plugin.on_load()
                assert server.frames == []
                task = asyncio.create_task(bound_call(plugin, comment=comment))
                if comment.strip():
                    await spin_until(lambda: 0.5 in clock.sleeps)
                    assert len(server.sends) == 1 and not task.done()
                    assert ledger.get(ledger_key()).status == "sent"
                    clock.advance(0.49)
                    for _ in range(20):
                        await asyncio.sleep(0)
                    assert len(server.sends) == 1
                    clock.advance(0.01)
                result = await asyncio.wait_for(task, 5)
                expected_sends = [expected] + ([[{"type": "text", "data": {"text": comment}}]] if comment.strip() else [])
                assert [entry["params"] for entry in server.sends] == [
                    {"message_type": "group", "group_id": TARGET_GROUP_ID, "message": segments}
                    for segments in expected_sends]
                assert result["status"] == "sent" and result["success"] and result["delivered"]
                assert result["target_message_id"] == "wire-target-1"
                row = ledger.get(ledger_key())
                assert (row.status, row.target_message_id) == ("sent", "wire-target-1")
                assert (row.comment_status, row.comment_message_id) == (("sent", "wire-target-2") if comment.strip() else ("", ""))
                assert (result["comment_status"], result["comment_message_id"]) == (row.comment_status, row.comment_message_id)
                assert server.observations == ([("sending", "", 0.0), ("sent", "sending", 0.5)] if comment.strip() else [("sending", "", 0.0)])
                assert ctx.maisaka.proactive.calls == [] and ctx.send.calls == [] and api_calls == []
                assert server.http_calls == []
                assert len(ctx.chat.streams) == 1, "目标没有宿主活动会话也可发送"
            finally:
                await plugin.on_unload()
                ledger.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fault,status,source_state,comment_state,send_count,stage", [
    ("before_send", "failed", "failed", "", 0, "source_send"),
    ("after_send", "unknown", "unknown", "", 1, "source_send"),
    ("no_response", "unknown", "unknown", "", 1, "source_send"),
    ("no_id", "unknown", "unknown", "", 1, "source_send"),
    ("wrong_account", "rejected", None, "", 0, "rejected"),
    ("wrong_record", "unknown", "unknown", "", 1, "source_send"),
    ("wrong_target", "unknown", "unknown", "", 1, "source_send"),
    ("reserve", "ledger_unavailable", None, "", 0, "source_persist"),
    ("mark_sent", "unknown", "sending", "", 1, "source_persist"),
    ("reserve_comment", "partial", "sent", "", 1, "comment_persist"),
    ("record_comment", "partial", "sent", "sending", 2, "comment_persist"),
])
def test_真实WS故障互证工具账本线路且重启不补发(tmp_path, fault, status, source_state, comment_state, send_count, stage):
    async def scenario():
        clock = FakeClock()
        raw = [text_segment("不可回显的合成源正文")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        storage = FaultLedger(ledger)
        storage.fail_next(fault)
        server = LocalOneBot(raw, clock, fault=fault, ledger=ledger)
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock, ledger=storage)
            try:
                task = asyncio.create_task(bound_call(plugin, comment="不可回显的合成评论"))
                if fault == "no_response":
                    await asyncio.wait_for(server.entered.wait(), 5)
                    clock.advance(10)
                elif fault in ("reserve_comment", "record_comment"):
                    await spin_until(lambda: 0.5 in clock.sleeps)
                    clock.advance(0.5)
                result = await asyncio.wait_for(task, 5)
                assert (result["status"], result["stage"], result["success"]) == (status, stage, False)
                assert len(server.sends) == send_count
                row = ledger.get(ledger_key())
                assert (None if row is None else row.status) == source_state
                if row is not None:
                    assert row.comment_status == comment_state
                if fault in ("mark_sent", "reserve_comment", "record_comment"):
                    assert result["sent"] and result["target_message_id"] == "wire-target-1"
                if fault == "record_comment":
                    assert (result["comment_status"], result["comment_message_id"]) == ("unknown", "wire-target-2")
                observed = json.dumps([result, ctx.logger.records], ensure_ascii=False)
                for secret in ("不可回显的合成源正文", "不可回显的合成评论", "synthetic-acceptance-only"):
                    assert secret not in observed
                assert ctx.send.calls == [] and api_calls == [] and server.http_calls == []
            finally:
                await plugin.on_unload()
                ledger.close()
            reopened = open_test_ledger(tmp_path, clock=clock)
            try:
                row = reopened.get(ledger_key())
                assert (None if row is None else row.status) == ("unknown" if source_state == "sending" else source_state)
                if row is not None:
                    assert row.comment_status == ("unknown" if comment_state == "sending" else comment_state)
                if source_state not in (None, "failed"):
                    before = len(server.frames)
                    second, second_ctx, second_api = assemble(tmp_path, url, raw, clock, ledger=reopened)
                    try:
                        result = await bound_call(second, comment="换评论也不补发")
                        assert result["status"] == ("already_forwarded" if source_state == "sent" else "unknown")
                        assert len(server.frames) == before
                        assert second_ctx.send.calls == [] and second_api == []
                    finally:
                        await second.on_unload()
            finally:
                reopened.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["关闭", "撤销目标", "切换连接", "卸载"])
def test_真实WS评论等待撤权保留本次结果与持久字段边界(tmp_path, change):
    async def scenario():
        clock, raw = FakeClock(), [text_segment("源")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(raw, clock, ledger=ledger)
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                task = asyncio.create_task(bound_call(plugin, comment="停止的评论"))
                await spin_until(lambda: 0.5 in clock.sleeps)
                if change == "卸载":
                    await plugin.on_unload()
                else:
                    config = plugin.config.model_dump()
                    if change == "关闭":
                        config["plugin"]["enabled"] = False
                    elif change == "撤销目标":
                        config["scope"]["target_groups"] = []
                    else:
                        config["snowluma"]["token_env_var"] = "OTHER_TOKEN_REFERENCE"
                    plugin.set_plugin_config(config)
                    await plugin.on_config_update("global", config, "test")
                    clock.advance(0.5)
                result = await asyncio.wait_for(task, 5)
                assert (result["status"], result["comment_status"], result["target_message_id"]) == ("partial", "failed", "wire-target-1")
                assert len(server.sends) == 1
                assert ctx.send.calls == [] and api_calls == [] and server.http_calls == []
            finally:
                await plugin.on_unload()
                ledger.close()
            reopened = open_test_ledger(tmp_path, clock=clock)
            second, _, _ = assemble(tmp_path, url, raw, clock, ledger=reopened)
            try:
                row = reopened.get(ledger_key())
                assert (row.status, row.comment_status, row.comment_message_id) == ("sent", "", "")
                before = len(server.frames)
                repeated = await bound_call(second, comment="不会补发")
                assert (repeated["status"], repeated["comment_status"]) == ("already_forwarded", "")
                assert len(server.frames) == before
            finally:
                await second.on_unload()
                reopened.close()
    asyncio.run(scenario())


def test_真实WS多客户端并存关闭隔离且另一目标独立(tmp_path):
    async def scenario():
        clock, raw = FakeClock(), [text_segment("仅指定目标")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(raw, clock, ledger=ledger)
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock, ledger=ledger)
            peer = AiohttpOneBotTransport(OneBotConnection(url, "ACCEPTANCE_TOKEN"), clock=clock,
                environ={"ACCEPTANCE_TOKEN": "synthetic-acceptance-only"})
            try:
                first, peer_reply = await asyncio.gather(bound_call(plugin), peer.get_login_info())
                assert first["target_message_id"] == "wire-target-1"
                assert peer_reply["data"]["user_id"] == SOURCE_ACCOUNT_ID
                second = await bound_call(plugin, target="30003")
                assert second["target_message_id"] == "wire-target-2"
                assert [item["params"]["group_id"] for item in server.sends] == [TARGET_GROUP_ID, "30003"]
                assert ledger.get(ledger_key()).target_message_id == "wire-target-1"
                assert ledger.get(ledger_key(target_group_id="30003")).target_message_id == "wire-target-2"
                assert ledger.get(ledger_key()).comment_status == ""
                assert 0.5 not in clock.sleeps, "评论参数缺省不等待也不占位"
                assert len(server.sockets) == 2
                await plugin.on_unload()
                assert (await peer.get_login_info())["data"]["user_id"] == SOURCE_ACCOUNT_ID
                assert len(server.sends) == 2 and len(server.sockets) == 2
                assert ctx.send.calls == [] and ctx.maisaka.proactive.calls == [] and api_calls == []
                assert server.http_calls == []
            finally:
                await plugin.on_unload()
                await peer.close()
                ledger.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("history_name", ["service_binding.json", "service_binding.json.tmp", "old-ledger.snapshot"])
def test_真实端点可用但服务历史痕迹使公共工具闭锁(tmp_path, history_name):
    async def scenario():
        evidence = tmp_path / history_name
        evidence.write_bytes(b"synthetic-history-not-new-install")
        clock, raw = FakeClock(), [text_segment("不发送")]
        server = LocalOneBot(raw, clock)
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock)
            try:
                result = await bound_call(plugin)
                assert result["status"] == "ledger_unavailable" and result["sent"] is False
                assert server.frames == [] and server.sockets == [] and server.http_calls == []
                assert ctx.send.calls == [] and api_calls == []
                assert evidence.read_bytes() == b"synthetic-history-not-new-install"
                assert not (tmp_path / LEDGER_FILE_NAME).exists()
                assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable)
            finally:
                await plugin.on_unload()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["关闭", "撤销目标", "卸载"])
def test_真实WS源账号查询等待后复验权限(tmp_path, change):
    async def scenario():
        clock, raw = FakeClock(), [text_segment("未提交源")]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(raw, clock, ledger=ledger)
        server.pause_login = 2
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                task = asyncio.create_task(bound_call(plugin, comment="不应发送"))
                await asyncio.wait_for(server.entered.wait(), 5)
                if change == "卸载":
                    # 先启动卸载使许可失效，释放模拟服务端以允许有限关闭。
                    stop = asyncio.create_task(plugin.on_unload())
                    await asyncio.sleep(0)
                    server.release.set()
                    await stop
                else:
                    config = plugin.config.model_dump()
                    if change == "关闭":
                        config["plugin"]["enabled"] = False
                    else:
                        config["scope"]["target_groups"] = []
                    plugin.set_plugin_config(config)
                    await plugin.on_config_update("global", config, "test")
                    server.release.set()
                result = await asyncio.wait_for(task, 5)
                assert result["status"] == "rejected" and result["sent"] is False
                assert server.sends == [] and ctx.send.calls == [] and api_calls == []
            finally:
                server.release.set()
                await plugin.on_unload()
                ledger.close()
            reopened = open_test_ledger(tmp_path, clock=clock)
            try:
                assert reopened.get(ledger_key()).status == "failed"
            finally:
                reopened.close()
    asyncio.run(scenario())


def test_真实WS事件与宿主Hook并存只有源群判断且沉默不发送(tmp_path):
    from tests.support import merged_forward_message
    async def scenario():
        clock, raw = FakeClock(), [forward_segment(forward_node())]
        ledger = open_test_ledger(tmp_path, clock=clock)
        server = LocalOneBot(raw, clock, ledger=ledger)
        async with endpoint(server.handler) as url:
            plugin, ctx, api_calls = assemble(tmp_path, url, raw, clock, ledger=ledger)
            try:
                assert (await bound_call(plugin))["status"] == "sent"
                before = len(server.frames)
                payload = merged_forward_message(message_id="m-source-1")
                await asyncio.gather(plugin.observe_received_message(**payload), plugin.observe_received_message(**payload))
                await spin_until(lambda: len(ctx.maisaka.proactive.calls) == 1)
                for connection in server.sockets:
                    await connection.send_json({"post_type": "message_sent", "message_id": "wire-target-1"})
                    await connection.send_json({"post_type": "message", "message_id": "m-source-1"})
                for _ in range(100):
                    await asyncio.sleep(0)
                assert len(ctx.maisaka.proactive.calls) == 1
                assert ctx.maisaka.proactive.calls[0]["stream_id"] == SOURCE_SESSION_ID
                assert len(server.frames) == before and len(server.sends) == 1
                assert ledger.get(ledger_key()).status == "sent"
                assert ctx.send.calls == [] and api_calls == []
            finally:
                await plugin.on_unload()
                ledger.close()
    asyncio.run(scenario())
