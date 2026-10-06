"""跨测试文件共享的替身与样本：几何常量、可控时钟、有界轮询与装配助手。

这里只放**默认 pytest 档**（不装 `aiohttp`）即可导入的公共部分：可控时钟、应答
信封、账本键与打开助手、公开工厂注入点、组件声明查询，以及多个用例共用的消息
样本。传输、客户端与内存平台装配各自的替身分别留在
`tests/snowluma_transport_support.py`、`tests/snowluma_client_support.py` 与
`tests/direct_support.py`，它们从这里取共享件，不再逐文件复制第二份实现。

组件声明一律经插件公共入口 `get_components()` 读取（Runner 的同款入口），不直接
访问私有方法或内部状态；账本断言只经 `open_ledger` 公开入口与真实 SQLite 文件。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from maimai_courier_plugin.shared.delivery import DeliveryKey
from maimai_courier_plugin.delivery_ledger import (
    LEDGER_FILE_NAME,
    DeliveryLedger,
    open_ledger,
)
from maimai_courier_plugin.plugin import (
    PLANNER_AFTER_RESPONSE_HOOK,
    RECEIVE_AFTER_PROCESS_HOOK,
    create_plugin,
)
from tests.fake_maibot_sdk import FakePluginContext

SOURCE_SESSION_ID = "s-source"
"""测试默认源群会话（宿主把 platform+account+scope+group 归一到会话 ID）。"""

SOURCE_GROUP_ID = "111"
"""测试默认转发分组内的源群。"""

TARGET_GROUP_ID = "222"
"""测试默认转发分组内的目标群（不同于源群）。"""

OTHER_TARGET_GROUP_ID = "30003"
"""默认分组内的第二个目标群（多目标与并存隔离用例用）。"""

DEFAULT_GROUP_NAME = "默认分组"
"""测试夹具默认转发分组名（只作用户标识与日志前缀，不参与判定）。"""

CONTROL_CHARACTER_GROUP_NAME = "研发\n甲\x00"
"""含换行与裸控制字符的分组名：日志与任务描述必须压平为单行。"""

SOURCE_ACCOUNT_ID = "10001"
"""测试默认机器人账号（路由与分组校验按此账号对齐）。"""

MESSAGE_ID = "m-source-1"
"""测试默认源消息 ID（宿主侧与 OneBot 侧同一标识）。"""

HTTP_URL = "http://127.0.0.1:3000"
"""SnowLuma 正向 HTTP API 地址（回环明文，组合根解析后交给传输）。"""

SENDER_USER_ID = 20002
"""OneBot 源消息记录里的发送者账号（文字节点署名只来自源消息）。"""

SENDER_NICKNAME = "群友甲"
"""OneBot 源消息记录里的发送者昵称（与群名片区分）。"""

SENDER_CARD = "名片甲"
"""OneBot 源消息记录里的发送者群名片（文字节点 name 优先取它）。"""


class FakeClock:
    """可控时钟：`now()` 返回虚拟时间，`sleep()` 登记唤醒点由 `advance()` 推进。

    实现插件侧 `clock.Clock` 协议（`now` + async `sleep`）；不真实等待，供
    「动作期限 / 回执轮询上限 / 0.5 秒评论延迟」的确定性测试使用。`sleeps`
    记录每次请求的秒数（含非正数），供用例断言是否经注入时钟登记过等待。
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        deadline = self._now + max(0.0, float(seconds))
        if deadline <= self._now:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((deadline, fut))
        try:
            await fut
        finally:
            self._sleepers = [item for item in self._sleepers if item[1] is not fut]

    def advance(self, seconds: float) -> None:
        """推进虚拟时间并唤醒到期的 `sleep`；未到期的继续等待。"""

        self._now += max(0.0, float(seconds))
        for deadline, fut in self._sleepers:
            if deadline <= self._now and not fut.done():
                fut.set_result(None)


async def spin_until(predicate: Callable[[], bool]) -> None:
    """有界让出调度、有限轮询本地测试事件，不作真实长等待。"""

    for _ in range(20000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("本地测试事件未就绪")


def envelope(data: Any, *, status: str = "ok", retcode: int = 0, message: str = "") -> dict[str, Any]:
    """OneBot 动作应答信封。"""

    return {"status": status, "retcode": retcode, "data": data, "message": message, "wording": ""}


def source_record(
    message: Any = None, *, sender: dict[str, Any] | None = None, message_id: Any = -12345,
) -> dict[str, Any]:
    """`get_msg` 的同账号源消息记录（OneBot 群消息形态，sender 署名只来自源消息）。"""

    record: dict[str, Any] = {
        "message_id": message_id,
        "message_type": "group",
        "group_id": 111,
        "user_id": SENDER_USER_ID,
        "message": [{"type": "text", "data": {"text": "甲"}}] if message is None else message,
        "sender": {"user_id": SENDER_USER_ID, "nickname": SENDER_NICKNAME, "card": SENDER_CARD},
    }
    if sender is not None:
        record["sender"] = sender
    return record


def ledger_key(
    *,
    message_id: str = MESSAGE_ID,
    target_group_id: str = TARGET_GROUP_ID,
    account_id: str = SOURCE_ACCOUNT_ID,
    source_group_id: str = SOURCE_GROUP_ID,
) -> DeliveryKey:
    """默认账本键（账号 + 源群 + 源消息 ID + 目标群）；用例只改需要区分的字段。"""

    return DeliveryKey(
        account_id=account_id,
        source_group_id=source_group_id,
        source_message_id=message_id,
        target_group_id=target_group_id,
    )


def open_test_ledger(data_dir: Path, *, clock: Any = None) -> DeliveryLedger:
    """打开 `data_dir` 下的账本用于断言；类型不符时直接断言失败。"""

    ledger = open_ledger(data_dir / LEDGER_FILE_NAME, clock=clock or FakeClock())
    assert isinstance(ledger, DeliveryLedger), f"账本应可打开: {ledger!r}"
    return ledger


def text_segment(text: str) -> dict[str, Any]:
    """文本段（宿主接收侧序列化形态）。"""

    return {"type": "text", "data": text}


def forward_segment(*nodes: dict[str, Any]) -> dict[str, Any]:
    """合并转发段（宿主接收侧序列化形态：`{"type": "forward", "data": [节点…]}`）。"""

    return {"type": "forward", "data": [dict(node) for node in nodes]}


def forward_node(
    *,
    content: list[dict[str, Any]] | None = None,
    user_id: str = "90001",
    user_nickname: str = "群友",
    user_cardname: str = "",
    message_id: str = "m-node-1",
) -> dict[str, Any]:
    """合并转发节点（键取宿主接收侧序列化名）。"""

    return {
        "user_id": user_id,
        "user_nickname": user_nickname,
        "user_cardname": user_cardname,
        "message_id": message_id,
        "content": [text_segment("节点原文")] if content is None else list(content),
    }


def source_stream(
    *,
    session_id: str = SOURCE_SESSION_ID,
    platform: str = "qq",
    group_id: str = SOURCE_GROUP_ID,
    account_id: str = SOURCE_ACCOUNT_ID,
    scope: str = "qq",
    is_group_session: bool = True,
) -> dict[str, Any]:
    """宿主 `chat.get_all_streams` 返回的单条会话字段。"""

    serialized = {
        "session_id": session_id,
        "stream_id": session_id,
        "platform": platform,
        "group_id": group_id,
        "account_id": account_id,
        "scope": scope,
        "is_group_session": is_group_session,
        "chat_type": "group" if is_group_session else "private",
    }
    if not is_group_session:
        serialized["group_id"] = ""
    return serialized


def source_message(
    *,
    message_id: str = MESSAGE_ID,
    session_id: str = SOURCE_SESSION_ID,
    group_id: str = SOURCE_GROUP_ID,
    user_id: str = "90001",
    raw_message: list[dict[str, Any]] | None = None,
    processed_plain_text: str = "源消息正文",
) -> dict[str, Any]:
    """宿主 `message.get_by_id` 返回形态的消息字典（可信保留链样本）。"""

    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": "qq",
        "message_info": {
            "group_info": {"group_id": group_id, "group_name": "测试源群"},
            "user_info": {"user_id": user_id, "user_nickname": "群友", "user_cardname": ""},
            "additional_config": {
                "account_id": SOURCE_ACCOUNT_ID,
                "platform_io_scope": "qq",
                "self_id": SOURCE_ACCOUNT_ID,
                "platform_io_account_id": SOURCE_ACCOUNT_ID,
                "napcat_message_type": "group",
                "client_type": "napcat",
            },
        },
        "raw_message": (
            [text_segment("源消息正文")] if raw_message is None else list(raw_message)
        ),
        "processed_plain_text": processed_plain_text,
    }


def components_of(plugin: Any) -> list[dict[str, Any]]:
    """读取插件注册的组件声明列表（Runner 的同款入口）。"""

    return list(plugin.get_components())


def find_hook(plugin: Any, hook: str) -> dict[str, Any]:
    """取订阅指定命名 Hook 的组件声明（宿主分发同一入口）。"""

    for component in components_of(plugin):
        if component.get("type") != "HOOK_HANDLER":
            continue
        if (component.get("metadata") or {}).get("hook") == hook:
            return component
    raise AssertionError(f"未注册 Hook {hook}；实际声明={components_of(plugin)}")


def enabled_config(
    *,
    source_groups: tuple[str, ...] = (SOURCE_GROUP_ID,),
    target_groups: tuple[str, ...] = (TARGET_GROUP_ID,),
    message_types: tuple[str, ...] = ("forward",),
    share_criteria: str | None = None,
    http_url: str = HTTP_URL,
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """「总开关开启 + 给定转发分组 + 回环 HTTP 直连」的配置覆盖值。

    `message_types` 默认 `("forward",)`，与配置模型默认一致；`share_criteria` 为
    `None` 时不写该键（回落模型默认原文），传入字符串则原样写入（含空白串）。
    转发分组有两种入口：默认（及 `source_groups=`/`target_groups=` 关键字）构造**单一
    默认分组**，两个关键字落入同一分组；`groups=` 按给定字典序列显式构造多分组，
    与 `CourierConfig(scope={"groups": [...]})` 的字段面一致；传入 `groups` 时另外
    两个关键字（`source_groups=`/`target_groups=`）被忽略。
    """

    auto: dict[str, Any] = {"message_types": list(message_types)}
    if share_criteria is not None:
        auto["share_criteria"] = share_criteria
    scope_groups = (
        [{"name": DEFAULT_GROUP_NAME, "source_groups": list(source_groups),
          "target_groups": list(target_groups)}]
        if groups is None else [
            {**group, "source_groups": list(group.get("source_groups") or []),
             "target_groups": list(group.get("target_groups") or [])}
            for group in groups
        ]
    )
    return {
        "plugin": {"enabled": True},
        "auto": auto,
        "scope": {"groups": scope_groups},
        "snowluma": {"http_url": http_url, "token": "合成令牌"},
    }


def make_plugin(
    data_dir: Path,
    *,
    config: dict[str, Any] | None = None,
    clock: Any = None,
    transport_factory: Any = None,
    environ: dict[str, str] | None = None,
    ledger_opener: Any = None,
) -> tuple[Any, FakePluginContext]:
    """经公开工厂注入时钟、传输与环境；账本默认由真实 `open_ledger` 打开。"""

    plugin = create_plugin(clock=clock or FakeClock(), transport_factory=transport_factory,
                           environ=environ, ledger_opener=ledger_opener)
    plugin.set_plugin_config(config or {})
    ctx = FakePluginContext(data_dir)
    plugin._set_context(ctx)
    return plugin, ctx


def issue_source_call(
    plugin: Any,
    *,
    session_id: str = SOURCE_SESSION_ID,
    message_id: str = MESSAGE_ID,
    target_group_id: str = TARGET_GROUP_ID,
    comment: Any = "",
    extra_arguments: dict[str, Any] | None = None,
    output_items: Any = None,
) -> dict[str, Any]:
    """经注册 Hook 签发一次性来源凭据，返回该工具调用最终收到的参数。"""

    arguments: dict[str, Any] = {
        "message_id": message_id,
        "target_group_id": target_group_id,
        "comment": comment,
    }
    arguments.update(extra_arguments or {})
    if output_items is None:
        output_items = [{
            "item_type": "FunctionCallItem",
            "meta": {"item_id": "item-1"},
            "tool_call": {
                "call_id": "call-1",
                "func_name": "forward_message",
                "args": arguments,
                "extra_content": None,
            },
        }]
    hook = find_hook(plugin, PLANNER_AFTER_RESPONSE_HOOK)
    handler = getattr(plugin, hook["metadata"]["handler_name"])
    result = asyncio.run(handler(session_id=session_id, output_items=output_items, item_schema_version=1))
    mutated_items = result["modified_kwargs"]["output_items"]
    if not isinstance(mutated_items, list) or not mutated_items:
        raise AssertionError(f"Hook 未返回可用的 output_items: {result!r}")
    tool_call = mutated_items[0].get("tool_call")
    if not isinstance(tool_call, dict):
        raise AssertionError(f"output_items 缺少 tool_call: {mutated_items!r}")
    return dict(tool_call.get("args") or {})
