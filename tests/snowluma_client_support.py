"""第4组公开宿主预检与固定 OneBot 动作的合成设施，不访问真实消息。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

from maimai_courier_plugin.config import CourierConfig
from maimai_courier_plugin.forward_precheck import precheck_forward_request
from maimai_courier_plugin.source_binding import SourceBindingTable
from maimai_courier_plugin.snowluma_transport import OneBotUnavailable


def host_message(message_id="-12345"):
    """02 的保留链样本：平台 ID 原文、可信会话和入站账号元数据。"""
    return {
        "message_id": message_id, "session_id": "s-test", "platform": "qq",
        "message_info": {
            "group_info": {"group_id": "111"},
            "additional_config": {"self_id": "10001", "platform_io_account_id": "10001",
                                  "napcat_message_type": "group", "client_type": "napcat"},
        },
        "raw_message": [{"type": "text", "data": "宿主正文不能作为平台身份证明"}],
    }


async def trusted_forward(message=None, *, message_id="-12345", target="222", extra=None):
    """经公开凭据表及本地预检取得 PreparedForward，而非手造可信标记。"""
    stored = host_message(message_id) if message is None else message
    async def streams(**kwargs):
        return [{"session_id": "s-test", "platform": "qq", "group_id": "111",
                 "account_id": "10001", "scope": "qq", "is_group_session": True}]
    async def get_by_id(query_id, **kwargs):
        assert query_id == message_id and kwargs == {"chat_id": "s-test", "include_binary_data": True}
        return stored
    ctx = SimpleNamespace(chat=SimpleNamespace(get_all_streams=streams), message=SimpleNamespace(get_by_id=get_by_id))
    bindings = SourceBindingTable()
    arguments = {"message_id": message_id, "target_group_id": target, "comment": ""}
    arguments.update(extra or {})
    items = bindings.sanitize_and_bind_output_items("s-test", [{
        "item_type": "FunctionCallItem", "tool_call": {"func_name": "forward_message", "args": arguments},
    }])
    config = CourierConfig(**{"plugin": {"enabled": True}, "scope": {"source_groups": ["111"], "target_groups": ["222", "111"]}})
    return await precheck_forward_request(ctx=ctx, config=config, bindings=bindings, arguments=items[0]["tool_call"]["args"])


class ScriptedOneBot:
    """只模拟已冻结四动作；可从公开动作回调控制查询和连接时序。"""

    def __init__(self):
        self.connection_generation = 1
        self.account = "10001"
        self.groups = [{"group_id": 222}]
        self.source = {"message_id": -12345, "message_type": "group", "group_id": 111,
                       "user_id": 20002, "message": [{"type": "text", "data": {"text": "甲"}}]}
        self.calls = []
        self.sent = []
        self.on_action = None
        self.closed = False

    async def action(self, name, value=None, *, expected_generation=None):
        self.calls.append((name, value, expected_generation))
        if self.closed or (expected_generation is not None and expected_generation != self.connection_generation):
            raise OneBotUnavailable("connection_changed", submitted=False)
        if self.on_action:
            result = await self.on_action(name, value)
            if result is not None:
                return result
        if name == "get_login_info":
            data = {"user_id": self.account}
        elif name == "get_group_list":
            data = self.groups
        elif name == "get_msg":
            if value == str(self.source["message_id"]):
                data = self.source
            else:
                data = {"message_id": value, "message_type": "group", "group_id": 222,
                        "user_id": 10001, "message_seq": 42}
        else:
            data = {"message_id": -23456}
        return {"status": "ok", "retcode": 0, "data": deepcopy(data)}

    async def get_login_info(self, *, expected_generation=None):
        return await self.action("get_login_info", expected_generation=expected_generation)

    async def get_group_list(self, *, expected_generation=None):
        return await self.action("get_group_list", expected_generation=expected_generation)

    async def get_msg(self, message_id, *, expected_generation=None):
        return await self.action("get_msg", message_id, expected_generation=expected_generation)

    async def send_msg(self, *, target_group_id, message, expected_generation, before_submit, stage=""):
        if self.closed or expected_generation != self.connection_generation or not before_submit():
            raise OneBotUnavailable("not_submitted", submitted=False)
        self.sent.append({"action": "send_msg", "params": {"message_type": "group", "group_id": target_group_id,
                                                         "message": deepcopy(list(message))}, "stage": stage})
        return await self.action("send_msg", expected_generation=expected_generation)

    async def close(self):
        self.closed = True
        self.connection_generation += 1


async def spin_until(predicate):
    """有界让出调度，测试不真实睡眠。"""
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("合成公开动作未到达预期位置")
