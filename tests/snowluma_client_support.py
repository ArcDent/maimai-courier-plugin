"""客户端与材料分派的合成替身：内存 `OneBotTransport` 与可信预检装配。

只经 `OneBotTransport` 协议方法（新 HTTP 无连接接口）与客户端/预检公开入口进入：
不访问客户端内部状态、不 mock 传输内部协作者、不导入 aiohttp（默认 pytest 档
可运行）、不触真实网络。应答信封与实测口径同构（`docs/snowluma-http-api-catalog.md`
第 6 节）：三个只读动作、四个发送动作都返回可核验 `message_id`（可为负），
`send_forward_msg` 额外返回 `res_id` / `forward_id`。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from maimai_courier_plugin.config import CourierConfig
from maimai_courier_plugin.forward_precheck import precheck_forward_request
from maimai_courier_plugin.source_binding import SourceBindingTable
from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
from tests.support import envelope, source_record

SOURCE_MESSAGE_ID = "-12345"
"""可信源消息的平台消息标识（负数：平台 ID 可为负，不得假设符号）。"""

TARGET_MESSAGE_ID = "-23456"
"""发送动作回执里的目标消息标识（与 res_id / forward_id 区分）。"""

CARD_RESOURCE_ID = "res-card-abc"
"""卡片段自带的资源引用：只供 `get_forward_msg` 展开查询按标识读取，发送载荷不得提交它。"""

DEFAULT_EXPANDED_CARD: dict[str, Any] = {"messages": [
    {"sender": {"user_id": "90001", "nickname": "群友", "card": ""},
     "message": [{"type": "text", "data": {"text": "卡片节点原文"}}]},
]}
"""`get_forward_msg` 展开查询的默认合成应答：单节点、署名可确认。

卡片全链用例（组合根、生命周期与回环验收）不注入自己的展开样本时按此应答构造
节点材料；需要特定展开形态的用例可整体替换 `MemoryOneBotTransport.expanded`。
"""

NESTED_EXPANDED_CARD: dict[str, Any] = {"messages": [
    {"sender": {"user_id": "90001", "card": "群友甲", "nickname": "甲"},
     "message": [{"type": "text", "data": {"text": "卡片节点正文"}}]},
    {"sender": {"user_id": "90002", "nickname": "乙"},
     "message": [{"type": "image", "data": {"url": "https://multimedia.example/x.png"}},
                 {"type": "forward", "data": {"id": "old-res-id", "content": [
                     {"type": "text", "data": {"text": "内层"}}]}}]},
]}
"""`get_forward_msg` 展开查询的嵌套应答：第二节点内容含内层 forward 段。

预识别（`SnowLumaClient.card_content_rejection`）与发送前类型检查（`prepare_source`
卡片分支）命中「嵌套内层卡片」时给出同一 reason（`type_check.NESTED_FORWARD_REASON`
单点定义）。用例整体替换 `MemoryOneBotTransport.expanded` 时建议 `deepcopy` 本常量，
避免共享可变样本；只读判定（`type_check.unsupported_card_content`）不改写输入。
"""


def host_message(message_id: str = SOURCE_MESSAGE_ID) -> dict[str, Any]:
    """保留链样本：平台 ID 原文、可信会话与入站账号元数据（预检输入）。"""

    return {
        "message_id": message_id, "session_id": "s-test", "platform": "qq",
        "message_info": {
            "group_info": {"group_id": "111"},
            "additional_config": {"self_id": "10001", "platform_io_account_id": "10001",
                                  "napcat_message_type": "group", "client_type": "napcat"},
        },
        "raw_message": [{"type": "text", "data": "宿主正文不能作为平台身份证明"}],
    }


async def trusted_forward(
    message: dict[str, Any] | None = None, *,
    message_id: str = SOURCE_MESSAGE_ID, target: str = "222", extra: dict[str, Any] | None = None,
) -> Any:
    """经公开凭据表与本地预检取得 `PreparedForward`，而非手造可信标记。"""

    stored = host_message(message_id) if message is None else message

    async def streams(**kwargs: Any) -> list[dict[str, Any]]:
        return [{"session_id": "s-test", "platform": "qq", "group_id": "111",
                 "account_id": "10001", "scope": "qq", "is_group_session": True}]

    async def get_by_id(query_id: str, **kwargs: Any) -> dict[str, Any]:
        assert query_id == message_id and kwargs == {"chat_id": "s-test", "include_binary_data": True}
        return stored

    ctx = SimpleNamespace(
        chat=SimpleNamespace(get_all_streams=streams),
        message=SimpleNamespace(get_by_id=get_by_id),
    )
    bindings = SourceBindingTable()
    arguments: dict[str, Any] = {"message_id": message_id, "target_group_id": target, "comment": ""}
    arguments.update(extra or {})
    items = bindings.sanitize_and_bind_output_items("s-test", [{
        "item_type": "FunctionCallItem",
        "tool_call": {"func_name": "forward_message", "args": arguments},
    }])
    config = CourierConfig(plugin={"enabled": True},
                           scope={"groups": [{"name": "客户端测试分组",
                                              "source_groups": ["111"],
                                              "target_groups": ["222", "111"]}]})
    return await precheck_forward_request(
        ctx=ctx, config=config, bindings=bindings, arguments=items[0]["tool_call"]["args"],
    )


def target_receipt(
    *, message_id: Any = TARGET_MESSAGE_ID, group_id: Any = 222,
    user_id: Any = 10001, message_seq: Any = 42,
) -> dict[str, Any]:
    """回查目标群消息记录：四项证据 + 正整数群消息序列。"""

    return {"message_id": message_id, "message_type": "group", "group_id": group_id,
            "user_id": user_id, "message_seq": message_seq}


class MemoryOneBotTransport:
    """内存传输替身：实现 `OneBotTransport` 协议七个动作与 `close`。

    `queries` 只读登记 `(action, params)`，`sent` 只登记发送动作的
    `{"action", "params"}`（`before_submit` 是提交闸门回调，不作为平台参数）；
    `on_action` 可对单个动作覆盖应答或抛 `OneBotUnavailable`，用于注入
    retcode=100、缺 ID、负 ID 与取消竞态。`expanded` 是 `get_forward_msg` 的
    展开查询应答：默认给合成节点样本，用例可整体替换。
    """

    def __init__(self) -> None:
        self.account = "10001"
        self.groups: list[dict[str, Any]] = [{"group_id": 222}]
        self.source = source_record()
        self.expanded: dict[str, Any] = deepcopy(DEFAULT_EXPANDED_CARD)
        self.queries: list[tuple[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.on_action: Any = None
        self.closed = False

    async def _reply(self, action: str, params: Any) -> dict[str, Any]:
        if self.on_action is not None:
            outcome = self.on_action(action, params)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            if outcome is not None:
                return outcome
        if action == "get_login_info":
            return envelope({"user_id": self.account})
        if action == "get_group_list":
            return envelope(deepcopy(self.groups))
        if action == "get_msg":
            if str(params["message_id"]) == str(self.source["message_id"]):
                return envelope(deepcopy(self.source))
            return envelope(target_receipt(message_id=params["message_id"]))
        if action == "get_forward_msg":
            # 展开查询只读应答：合成节点序列原样上交。
            return envelope(deepcopy(self.expanded))
        return envelope({"message_id": TARGET_MESSAGE_ID, "res_id": CARD_RESOURCE_ID,
                         "forward_id": "fwd-abc"})

    async def get_login_info(self) -> dict[str, Any]:
        self.queries.append(("get_login_info", {}))
        return await self._reply("get_login_info", {})

    async def get_group_list(self) -> dict[str, Any]:
        self.queries.append(("get_group_list", {}))
        return await self._reply("get_group_list", {})

    async def get_msg(self, message_id: str) -> dict[str, Any]:
        self.queries.append(("get_msg", {"message_id": message_id}))
        return await self._reply("get_msg", {"message_id": message_id})

    async def get_forward_msg(self, id: str) -> dict[str, Any]:
        """按源卡片标识展开查询合并转发节点：只读，不设提交许可闸门。"""

        self.queries.append(("get_forward_msg", {"id": id}))
        return await self._reply("get_forward_msg", {"id": id})

    async def send_msg(
        self, *, target_group_id: str, message: Any, before_submit: Any,
    ) -> dict[str, Any]:
        if self.closed or not before_submit():
            raise OneBotUnavailable("permission", submitted=False)
        params = {"message_type": "group", "group_id": target_group_id, "message": deepcopy(list(message))}
        self.sent.append({"action": "send_msg", "params": params})
        return await self._reply("send_msg", params)

    async def send_group_forward_msg(
        self, *, target_group_id: str, messages: Any, before_submit: Any,
    ) -> dict[str, Any]:
        """卡片展开节点重组投递：只提交目标群与展开查询构造的节点数组。"""

        if self.closed or not before_submit():
            raise OneBotUnavailable("permission", submitted=False)
        params = {"group_id": target_group_id, "messages": deepcopy(list(messages))}
        self.sent.append({"action": "send_group_forward_msg", "params": params})
        return await self._reply("send_group_forward_msg", params)

    async def send_forward_msg(
        self, *, target_group_id: str, messages: Any, before_submit: Any,
    ) -> dict[str, Any]:
        if self.closed or not before_submit():
            raise OneBotUnavailable("permission", submitted=False)
        params = {"group_id": target_group_id, "messages": deepcopy(list(messages))}
        self.sent.append({"action": "send_forward_msg", "params": params})
        return await self._reply("send_forward_msg", params)

    async def close(self) -> None:
        self.closed = True


