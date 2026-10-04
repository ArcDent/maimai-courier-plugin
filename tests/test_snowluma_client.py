"""第4组：可信准备、固定群动作与回执的公开入口验收；全部载荷为合成。"""

import asyncio
from dataclasses import replace
from copy import deepcopy

import pytest

from maimai_courier_plugin.snowluma_client import SnowLumaClient, PrepareRejected, PreparedSource
from tests.snowluma_client_support import ScriptedOneBot, trusted_forward
from tests.support import FakeClock
from tests.snowluma_client_support import spin_until


@pytest.mark.parametrize("stage", ["source", "comment"])
@pytest.mark.parametrize("outcome", ["sent", "unknown", "failed"])
def test_工作结束释放临时正文而结果仍可定位(stage, outcome):
    import gc
    import weakref
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    class TrackedText(str):
        """仅为垃圾回收可观测性保留弱引用；不改变字符串行为。"""
    async def scenario():
        transport = ScriptedOneBot()
        transport.source["message"] = [{"type": "text", "data": {"text": TrackedText("秘密正文")}}]
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        body_ref = weakref.ref(prepared.material.segments[0]["data"]["text"])
        comment = TrackedText("秘密评论")
        comment_ref = weakref.ref(comment)
        async def reply(name, value):
            if outcome == "unknown" and name == "send_msg":
                return {"status": "async"}
        transport.on_action = reply
        if stage == "source":
            result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: outcome != "failed")
        else:
            result = await client.send_comment(CommentSendRequest(prepared, comment), before_submit=lambda: outcome != "failed")
        assert result.status == outcome
        assert "秘密" not in repr(prepared) + repr(result)
        # 调用者释放本次材料；协议替身的线路记录也不再持有正文。
        del prepared, comment
        transport.sent.clear()
        transport.source["message"] = []
        for _ in range(30):
            await asyncio.sleep(0)
        gc.collect()
        assert body_ref() is None and comment_ref() is None
        if outcome == "sent":
            assert result.target_message_id == "-23456"
        await client.close()
    asyncio.run(scenario())


def test_关闭异常只报告安全类别不回显外部载荷(caplog):
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
    async def scenario():
        class FailedClose(ScriptedOneBot):
            async def close(self):
                raise RuntimeError("TOKEN-test 秘密正文 秘密评论 resource-test-A https://images.example.org/?signature=SECRET")
        client = SnowLumaClient(transport=FailedClose(), clock=FakeClock(), config_generation=1)
        with pytest.raises(OneBotUnavailable) as caught:
            await client.close()
        for value in ("TOKEN-test", "秘密正文", "秘密评论", "resource-test-A", "signature=SECRET"):
            assert value not in str(caught.value) + caplog.text
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
@pytest.mark.parametrize("response", [
    {"status": "async", "data": {"message_id": -23456}},
    {"status": "ok", "data": {}},
    {"status": "ok", "data": {"message_id": True}},
    {"status": "unexpected", "data": {"message_id": -23456}},
    {"status": "failed", "retcode": 100, "wording": "TOKEN-test-秘密正文-秘密评论-resource-test-A"},
    {"status": "ok", "retcode": True, "data": {"message_id": -23456}},
    {"status": "ok", "retcode": "0", "data": {"message_id": -23456}},
    {"status": "ok", "retcode": 1, "data": {"message_id": -23456}},
    {"status": "ok", "post_type": "message", "data": {"message_id": -23456}},
    {"status": "ok", "data": []},
])
def test_受理或契约不足不伪报成功失败也不另发评论(stage, response, caplog):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        async def reply(name, value):
            if name == "send_msg":
                return response
        transport.on_action = reply
        if stage == "source":
            result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        else:
            result = await client.send_comment(CommentSendRequest(prepared, "秘密评论"), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("unknown", "")
        clock.advance(20)
        for _ in range(20):
            await asyncio.sleep(0)
        assert [call["stage"] for call in transport.sent] == [stage]
        assert [call[1] for call in transport.calls if call[0] == "get_msg"] == ["-12345"]
        for sensitive in ("TOKEN-test", "秘密正文", "秘密评论", "resource-test-A"):
            assert sensitive not in repr(result) + caplog.text
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
@pytest.mark.parametrize("point", ["account", "submit", "receipt"])
def test_取消按实际提交边界分类且不迟到发送(stage, point, caplog):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        action = {"account": "get_login_info", "submit": "send_msg", "receipt": "get_msg"}[point]
        async def reply(name, value):
            if name == action:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # 模拟外部依赖取消后仍迟到返回；客户端最后许可必须已失效。
                    await release.wait()
                finally:
                    finished.set()
        transport.on_action = reply
        if stage == "source":
            task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        else:
            task = asyncio.create_task(client.send_comment(CommentSendRequest(prepared, "秘密评论"), before_submit=lambda: True))
        await entered.wait()
        task.cancel()
        await spin_until(task.done)
        result = task.result()
        assert result.status == ("failed" if point == "account" else "unknown")
        assert result.target_message_id == ("-23456" if point == "receipt" else "")
        release.set()
        await finished.wait()
        for _ in range(30):
            await asyncio.sleep(0)
        assert len(transport.sent) == (0 if point == "account" else 1)
        assert "秘密评论" not in repr(result) + caplog.text
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
@pytest.mark.parametrize("point", ["account", "submit", "receipt"])
def test_异常不回显载荷并保留可定位身份(stage, point, caplog):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        sensitive = "TOKEN-test 秘密正文 秘密评论 resource-test-A https://images.example.org/a?signature=SECRET"
        entered = asyncio.Event()
        async def reply(name, value):
            if name == {"account": "get_login_info", "submit": "send_msg", "receipt": "get_msg"}[point]:
                entered.set()
                if point == "receipt":
                    raise RuntimeError(sensitive)
                raise OneBotUnavailable(sensitive, submitted=point != "account")
        transport.on_action = reply
        if stage == "source":
            task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        else:
            task = asyncio.create_task(client.send_comment(CommentSendRequest(prepared, "秘密评论"), before_submit=lambda: True))
        await entered.wait()
        if point == "receipt":
            for _ in range(20):
                await asyncio.sleep(0)
            clock.advance(3)
        result = await task
        assert result.status == ("failed" if point == "account" else "unknown")
        assert (prepared.source_message_id, prepared.target_group_id) == ("-12345", "222")
        for value in sensitive.split():
            assert value not in repr(result) + caplog.text
        assert len(transport.sent) == (0 if point == "account" else 1)
        await client.close()
    asyncio.run(scenario())


def test_不合作关闭共享五秒预算且重复关闭不重置期限():
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
    async def scenario():
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowClose(ScriptedOneBot):
            async def close(self):
                entered.set()
                try:
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            continue
                finally:
                    finished.set()
        clock = FakeClock()
        client = SnowLumaClient(transport=SlowClose(), clock=clock, config_generation=1)
        task = asyncio.create_task(client.close())
        await entered.wait()
        try:
            clock.advance(5)
            await spin_until(task.done)
            with pytest.raises(OneBotUnavailable, match="close"):
                task.result()
            retry = asyncio.create_task(client.close())
            await spin_until(retry.done)
            with pytest.raises(OneBotUnavailable, match="close"):
                retry.result()
            assert clock.now() == 5
        finally:
            release.set()
            await finished.wait()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["prepare", "source", "comment"])
def test_关闭结束本实例等待且不影响其他客户端(stage):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        trusted = await trusted_forward()
        prepared = await client.prepare_source(trusted)
        entered, release = asyncio.Event(), asyncio.Event()
        async def response(name, value):
            if name == "get_login_info":
                entered.set()
                await release.wait()
        transport.on_action = response
        if stage == "prepare":
            task = asyncio.create_task(client.prepare_source(trusted))
        elif stage == "source":
            task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        else:
            task = asyncio.create_task(client.send_comment(CommentSendRequest(prepared, "评论"), before_submit=lambda: True))
        await entered.wait()
        try:
            await client.close()
            await client.close()
            await spin_until(task.done)
            assert isinstance(task.result(), PrepareRejected) if stage == "prepare" else task.result().status == "failed"
            assert transport.sent == [] and transport.closed
            other = SnowLumaClient(transport=ScriptedOneBot(), clock=clock, config_generation=1)
            assert isinstance(await other.prepare_source(trusted), PreparedSource)
            await other.close()
        finally:
            release.set()
            await task
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
def test_本地不透明回执只有候选ID不能确认发送(stage):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport = ScriptedOneBot()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        async def response(name, value):
            if name == "send_msg":
                return {"status": "ok", "data": {"message_id": "local-test-opaque"}}
            if name == "get_msg":
                return {"status": "ok", "data": {"message_id": "local-test-opaque", "message_type": "group",
                                                 "group_id": 222, "user_id": 10001}}
        transport.on_action = response
        if stage == "source":
            result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        else:
            result = await client.send_comment(CommentSendRequest(prepared, "评论"), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("unknown", "local-test-opaque")
        assert [call[1] for call in transport.calls if call[0] == "get_msg"] == ["-12345", "local-test-opaque"]
        assert len(transport.sent) == 1
    asyncio.run(scenario())


def test_回执末次等待不超过三秒剩余预算():
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        entered = asyncio.Event()
        queries = []
        async def response(name, value):
            if name == "get_msg":
                queries.append((value, clock.now()))
                if len(queries) == 1:
                    entered.set()
                    await clock.sleep(2.8)
                    return {"status": "failed"}
        transport.on_action = response
        task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        await entered.wait()
        clock.advance(2.8)
        await spin_until(lambda: any(0 < seconds < 0.21 for seconds in clock.sleeps))
        assert not task.done() and queries == [("-23456", 0.0)]
        clock.advance(0.2)
        await spin_until(task.done)
        assert task.result().status == "unknown"
        assert queries == [("-23456", 0.0)] and len(transport.sent) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
def test_只有目标回执四项证据齐全才确认发送(stage):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport = ScriptedOneBot()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        if stage == "source":
            result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        else:
            result = await client.send_comment(CommentSendRequest(prepared, "评论"), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("sent", "-23456")
        assert [call for call in transport.calls if call[0] == "get_msg"] == [("get_msg", "-12345", 1), ("get_msg", "-23456", 1)]
        assert len(transport.sent) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("patch", [
    {"message_id": -23457}, {"group_id": 333}, {"user_id": 99999},
    {"message_seq": True}, {"message_seq": False}, {"message_seq": 0}, {"message_seq": -1},
    {"message_seq": "42"}, {"message_seq": None}, {"message_type": "private"}, {"message_type": None},
])
def test_回执错记录错群错账号或非正整数序列均未知(patch):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest
    async def scenario():
        transport = ScriptedOneBot()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        async def response(name, value):
            if name == "get_msg":
                data = {"message_id": -23456, "group_id": 222, "user_id": 10001, "message_type": "group", "message_seq": 42}
                data.update(patch)
                return {"status": "ok", "data": data}
        transport.on_action = response
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("unknown", "-23456")
        assert len(transport.sent) == 1
        assert len([call for call in transport.calls if call[0] == "get_msg" and call[1] == "-23456"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["blocked", "missing", "late"])
def test_回执查询三秒总预算且仅查本次ID不叠加十秒(mode):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        queries = []
        async def response(name, value):
            if name == "get_msg":
                queries.append((value, clock.now()))
                if mode == "blocked":
                    await asyncio.Event().wait()
                if mode == "late":
                    await clock.sleep(2.8)
                return {"status": "failed", "retcode": 1}
        transport.on_action = response
        task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        await spin_until(lambda: len(queries) == 1)
        for _ in range(6):
            clock.advance(0.5)
            for _ in range(40):
                await asyncio.sleep(0)
        await spin_until(task.done)
        result = task.result()
        assert (result.status, result.target_message_id) == ("unknown", "-23456")
        assert 1 <= len(queries) <= 8
        assert all(message_id == "-23456" and time < 3 for message_id, time in queries)
        assert all(after[1] - before[1] >= 0.5 for before, after in zip(queries, queries[1:]))
        assert len(transport.sent) == 1 and clock.now() == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
@pytest.mark.parametrize("fault", ["account", "disconnect_after_login", "config", "permission_after_login"])
def test_每阶段取得执行机会后核对账号并固定配置连接(stage, fault):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport = ScriptedOneBot()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        if fault == "config":
            client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=2)
        allowed = True
        async def change(name, value):
            nonlocal allowed
            if name == "get_login_info":
                if fault == "account":
                    transport.account = "99999"
                elif fault == "disconnect_after_login":
                    transport.connection_generation += 1
                elif fault == "permission_after_login":
                    allowed = False
                return {"status": "ok", "data": {"user_id": transport.account}}
        transport.on_action = change
        if stage == "source":
            result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: allowed)
        else:
            result = await client.send_comment(CommentSendRequest(prepared, "评论"), before_submit=lambda: allowed)
        assert result.status == "failed"
        assert transport.sent == []
    asyncio.run(scenario())


def test_准备和发送串行排队且等待包含在十秒内():
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest
    async def scenario():
        transport, clock = ScriptedOneBot(), FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        trusted = await trusted_forward()
        prepared = await client.prepare_source(trusted)
        entered, release = asyncio.Event(), asyncio.Event()
        async def gate(name, value):
            if name == "get_login_info":
                entered.set()
                await release.wait()
        transport.on_action = gate
        first = asyncio.create_task(client.prepare_source(trusted))
        await entered.wait()
        second = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len([call for call in transport.calls if call[0] == "get_login_info"]) == 2
        clock.advance(10)
        await spin_until(lambda: first.done() and second.done())
        assert isinstance(first.result(), PrepareRejected)
        assert second.result().status == "failed" and transport.sent == []
        release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["source", "comment"])
def test_源与评论只调用一次固定群动作且评论只作文本(stage):
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest, CommentSendRequest
    async def scenario():
        transport = ScriptedOneBot()
        transport.source["message"] = [{"type": "forward", "data": {"id": "resource-test-A"}}]
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        comment = '[CQ:at,qq=all] $(命令) {"group_id":999}'
        if stage == "source":
            await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
            expected = [{"type": "forward", "data": {"id": "resource-test-A"}}]
        else:
            await client.send_comment(CommentSendRequest(prepared, comment), before_submit=lambda: True)
            expected = [{"type": "text", "data": {"text": comment}}]
        assert transport.sent == [{"action": "send_msg", "params": {"message_type": "group", "group_id": "222", "message": expected}, "stage": stage}]
    asyncio.run(scenario())


@pytest.mark.parametrize("comment", ["", " \n\t\u3000"])
def test_空白评论在任何平台查询前结束(comment):
    from maimai_courier_plugin.shared.send_steps import CommentSendRequest
    async def scenario():
        transport = ScriptedOneBot()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        transport.calls.clear()
        result = await client.send_comment(CommentSendRequest(prepared, comment), before_submit=lambda: True)
        assert result.status == "failed"
        assert transport.calls == [] and transport.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize("segments", [
    [{"type": "image", "data": {"url": "https://images.example.org/a.png"}}],
    [{"type": "image", "data": {"url": "https://images.example.org/a.png"}}, {"type": "image", "data": {"url": "https://images.example.org/b.png"}}],
    [{"type": "text", "data": {"text": "甲\n"}}, {"type": "image", "data": {"url": "https://images.example.org/a.png"}}, {"type": "image", "data": {"url": "https://images.example.org/b.png"}}, {"type": "text", "data": {"text": "乙"}}],
])
def test_可信图片与图文完整重放且不访问媒体(segments, monkeypatch):
    from maimai_courier_plugin.send_material import source_outbound_segments
    import socket
    import builtins
    def forbidden(*args, **kwargs):
        raise AssertionError("纯材料准备不应访问本机文件或媒体地址")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    async def scenario():
        transport = ScriptedOneBot()
        transport.source["message"] = deepcopy(segments)
        prepared = await SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1).prepare_source(await trusted_forward())
        assert isinstance(prepared, PreparedSource)
        assert source_outbound_segments(prepared.material) == tuple(segments)
        assert transport.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    {}, {"file": "/private/image.png"}, {"url": "file:///private/image.png"},
    {"url": "http://images.example.org/x"}, {"url": "https://127.0.0.1/x"},
    {"url": "https://10.0.0.1/x"}, {"url": "https://169.254.169.254/x"},
    {"url": "https://[::1]/x"}, {"url": "https://localhost/x"},
    {"url": "https://2130706433/x"}, {"url": "https://0x7f000001/x"},
    {"url": "https://127.1/x"}, {"url": "https://user:secret@images.example.org/x"},
    {"url": "https://images.example.org\\@127.0.0.1/x"},
    {"url": "https://images.example.org/\u200bx"}, {"url": "https://100.64.0.1/x"},
])
def test_媒体不可重放在准备阶段拒绝不读文件或内网(data, monkeypatch):
    from maimai_courier_plugin.send_material import build_source_material
    import socket
    import builtins
    def forbidden(*args, **kwargs):
        raise AssertionError("拒绝材料不能读取文件或访问网络")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    result = build_source_material([{"type": "text", "data": {"text": "头"}}, {"type": "image", "data": data}])
    assert isinstance(result, PrepareRejected)
    assert "secret" not in result.reason


def test_普通提及复用呈现规则保留边界换行且不改输入():
    from maimai_courier_plugin.send_material import build_source_material, source_outbound_segments
    long_name = "长" * 500 + "\n\x00[CQ:at,qq=all]"
    raw = [
        {"type": "text", "data": {"text": "甲\n"}},
        {"type": "text", "data": {"text": "乙"}},
        {"type": "at", "data": {"qq": "20002", "card": long_name}},
        {"type": "at", "data": {"qq": 20003}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": "\n尾"}},
    ]
    original = deepcopy(raw)
    material = build_source_material(raw)
    assert not isinstance(material, PrepareRejected)
    assert source_outbound_segments(material) == (
        {"type": "text", "data": {"text": "甲\n"}},
        {"type": "text", "data": {"text": "乙［提及：" + "长" * 500 + " [CQ:at,qq=all]（20002）］［提及：20003］［提及：全体成员］\n尾"}},
    )
    assert raw == original


@pytest.mark.parametrize("segment", [
    {"type": "at", "data": {}}, {"type": "at", "data": {"qq": True}},
    {"type": "at", "data": {"qq": ""}}, {"type": "at", "data": {"qq": "0"}},
    {"type": "at", "data": {"qq": "谁"}}, {"type": "reply", "data": {"id": "secret"}},
    {"type": "record", "data": {}}, {"type": "file", "data": {}},
])
def test_普通提及未知或不支持段不丢段降级(segment):
    from maimai_courier_plugin.send_material import build_source_material
    result = build_source_material([{"type": "text", "data": {"text": "头"}}, segment])
    assert isinstance(result, PrepareRejected)


def test_卡片只取原资源引用并忽略全部内部形状():
    async def scenario():
        transport = ScriptedOneBot()
        transport.source["message"] = [{"type": "forward", "data": {"id": "resource-test-A", "content": [
            {"type": "reply"}, {"type": "at", "data": {}}, {"type": "unknown", "data": "内部私密"},
        ]}}]
        original = deepcopy(transport.source)
        prepared = await SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1).prepare_source(await trusted_forward())
        assert isinstance(prepared, PreparedSource)
        from maimai_courier_plugin.send_material import source_outbound_segments
        assert source_outbound_segments(prepared.material) == ({"type": "forward", "data": {"id": "resource-test-A"}},)
        assert prepared.source_message_id == "-12345"
        assert transport.source == original and transport.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize("segments", [
    [{"type": "forward", "data": {}}],
    [{"type": "forward", "data": {"id": ""}}],
    [{"type": "forward", "data": {"id": "r1"}}, {"type": "forward", "data": {"id": "r2"}}],
    [{"type": "forward", "data": {"id": "r1"}}, {"type": "text", "data": {"text": "外文"}}],
])
def test_卡片资源缺失或顶层混排整条拒绝(segments):
    async def scenario():
        transport = ScriptedOneBot()
        transport.source["message"] = segments
        prepared = await SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1).prepare_source(await trusted_forward())
        assert isinstance(prepared, PrepareRejected)
        assert transport.sent == []
    asyncio.run(scenario())


def test_准备按账号名册源记录顺序只读且保留查询身份():
    async def scenario():
        transport = ScriptedOneBot()
        trusted = await trusted_forward()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=7)
        prepared = await client.prepare_source(trusted)
        assert isinstance(prepared, PreparedSource)
        assert (prepared.account_id, prepared.source_group_id, prepared.source_message_id, prepared.target_group_id) == ("10001", "111", "-12345", "222")
        assert (prepared.connection_generation, prepared.config_generation) == (1, 7)
        assert transport.calls == [("get_login_info", None, None), ("get_group_list", None, 1), ("get_msg", "-12345", 1)]
        assert transport.sent == []
        assert trusted.arguments.message_id == "-12345"
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["account", "target", "same_group", "private", "wrong_group", "wrong_id", "missing", "mapping"])
def test_准备身份不符或材料来源无法确认拒绝且零发送(fault):
    async def scenario():
        transport = ScriptedOneBot()
        trusted = await trusted_forward()
        if fault == "account":
            transport.account = "99999"
        elif fault == "target":
            transport.groups = [{"group_id": 333}]
        elif fault == "same_group":
            trusted = replace(trusted, arguments=replace(trusted.arguments, target_group_id="111"))
        elif fault == "private":
            transport.source["message_type"] = "private"
        elif fault == "wrong_group":
            transport.source["group_id"] = 333
        elif fault == "wrong_id":
            async def wrong(name, value):
                if name == "get_msg":
                    return {"status": "ok", "data": {"message_id": -99, "group_id": 111, "message_type": "group", "message": "越权正文"}}
            transport.on_action = wrong
        elif fault == "missing":
            async def missing(name, value):
                if name == "get_msg":
                    return {"status": "failed", "wording": "越权正文"}
            transport.on_action = missing
        else:
            trusted = replace(trusted, onebot_message_id="")
        result = await SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=7).prepare_source(trusted)
        assert isinstance(result, PrepareRejected)
        assert "越权正文" not in result.reason
        assert transport.sent == []
        if fault in ("account", "target", "same_group", "mapping"):
            assert not any(call[0] == "get_msg" for call in transport.calls)
    asyncio.run(scenario())
