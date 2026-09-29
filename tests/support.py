"""测试装配助手：组件声明查询等跨用例公共装配。

组件声明一律经插件公共入口 `get_components()` 读取（Runner 的同款入口），
不直接访问私有方法或内部状态。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from delivery_ledger import (
    LEDGER_FILE_NAME,
    DeliveryKey,
    DeliveryLedger,
    LedgerUnavailableError,
    open_ledger,
)
from forward_delivery import SourceSendResult
from plugin import (
    PLANNER_AFTER_RESPONSE_HOOK,
    RECEIVE_AFTER_PROCESS_HOOK,
    CourierPlugin,
    create_plugin,
)

from tests.fake_maibot_sdk import FakeChatCapability, FakePluginContext

# 测试几何常量：会话、群号、平台与账号由夹具与用例共用。定义必须早于各构造函数，
# 因为默认参数值在**定义时**求值。
# 第 3 组（工具权限与内容预检）起的用例经宿主 `maisaka.planner.after_response`
# 注册 Hook 获取工具参数，再调用 `forward_message`——生产链路只信任该 Hook 签发的
# 一次性凭据。

SOURCE_SESSION_ID = "s-source-10001"
"""测试默认源群会话：宿主把 `platform+account+scope+group` 归一到会话 ID。"""

SOURCE_GROUP_ID = "10001"
"""测试默认白名单源群。"""

TARGET_GROUP_ID = "20002"
"""测试默认白名单目标群（不同于源群）。"""

SOURCE_PLATFORM = "qq"
"""测试默认平台：宿主 QQ 平台名（非 QQ 平台一律拒绝）。"""

SOURCE_ACCOUNT_ID = "10000"
"""测试默认机器人账号（路由与白名单校验按此账号对齐）。"""

SOURCE_SCOPE = "qq"
"""测试默认路由作用域（宿主 `additional_config` 里的 platform_io_scope 别名）。"""

HOST_ACCOUNT_ID_KEYS: tuple[str, ...] = (
    "platform_io_account_id",
    "account_id",
    "self_id",
    "bot_account",
)
"""宿主路由键工厂的账号别名（MaiBot `src/platform_io/route_key_factory.py:28-33`）。"""

HOST_SCOPE_KEYS: tuple[str, ...] = (
    "platform_io_scope",
    "route_scope",
    "adapter_scope",
    "connection_id",
)
"""宿主路由键工厂的作用域别名（MaiBot `src/platform_io/route_key_factory.py:34-39`）。"""


class FakeClock:
    """可控时钟：`now()` 返回虚拟时间，`sleep()` 登记唤醒点由 `advance()` 推进。

    不真实等待，供「有界等待 / 0.5 秒评论延迟 / 超时」的确定性测试使用；实现
    插件侧 `clock.Clock` 协议（`now` + async `sleep`）。`sleeps` 记录每次请求的
    秒数（含非正数），供用例断言插件是否经注入时钟登记过等待。
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


def make_plugin(
    data_dir: Path,
    *,
    config: dict[str, Any] | None = None,
    clock: FakeClock | None = None,
    send_source: Any = None,
    open_ledger: Any = None,
    storage_fault: bool = False,
) -> tuple[CourierPlugin, FakePluginContext]:
    """构造绕过 MaiBot 运行时的插件实例：注入可控时钟、替身发送步骤与上下文。

    上下文经 SDK 的 `_set_context()` 注入（Runner 的同款入口，`ctx` 本身只读）；
    `config` 为 None 时使用模型默认值（默认关闭）；`send_source` 为本组接缝替身
    （需要替身上下文记录的替身由本函数自动 `bind`）。返回插件与上下文，
    供用例同时断言插件结果与宿主能力调用。
    """

    plugin = create_plugin(
        clock=clock or FakeClock(),
        send_source=send_source,
        open_ledger=open_ledger,
    )
    plugin.set_plugin_config(config or {})
    ctx = FakePluginContext(data_dir, storage_fault=storage_fault)
    plugin._set_context(ctx)
    if send_source is not None and hasattr(send_source, "bind"):
        send_source.bind(ctx)
    return plugin, ctx


def prepare_source(
    ctx: Any,
    *,
    raw_message: list[dict[str, Any]] | None = None,
    message_id: str = "m-source-1",
    **stream_overrides: Any,
) -> None:
    """装配一条可信来源会话与其源消息（宿主查询面替身）。"""

    ctx.chat.streams = [source_stream(**stream_overrides)]
    ctx.message.store = {
        (SOURCE_SESSION_ID, message_id): source_message(message_id=message_id, raw_message=raw_message),
    }


class RecordingSendStep:
    """替身源发送步骤：经替身 `ctx.send` 记录一次发送，返回可控结果。

    第 4 组不接通真实发送，因此用本替身表达「进入源发送」：按材料种类调用替身的
    `ctx.send.forward` / `ctx.send.text`（`ctx.send.calls` 就是发送进入次数）。真实
    通路（含图片、详细回执）在第 5 组替换本替身。

    控制面：`status` / `target_message_id` / `reason` 覆盖返回结果；`on_enter` 在
    记录发送前执行（供事务纪律用例在「网络等待」期间用第二个连接观察账本）；
    `ctx.send.hold` 由用例在事件循环内赋值即可让发送悬停在途。
    """

    def __init__(
        self,
        *,
        status: str = "sent",
        target_message_id: str = "m-delivered-1",
        reason: str = "",
        on_enter: Any = None,
    ) -> None:
        self.status = status
        self.target_message_id = target_message_id
        self.reason = reason
        self.on_enter = on_enter
        self.entries: list[Any] = []
        self._ctx: Any = None

    def bind(self, ctx: Any) -> "RecordingSendStep":
        """绑定替身上下文（`make_plugin` 自动调用）。"""

        self._ctx = ctx
        return self

    async def __call__(self, prepared: Any) -> SourceSendResult:
        self.entries.append(prepared)
        if self.on_enter is not None:
            self.on_enter()
        if prepared.material.kind == "forward":
            await self._ctx.send.forward(
                list(prepared.material.nodes),
                prepared.target.session_id,
                return_details=True,
            )
        else:
            await self._ctx.send.text(
                "源消息",
                prepared.target.session_id,
                return_details=True,
            )
        return SourceSendResult(
            status=self.status,
            target_message_id=self.target_message_id,
            reason=self.reason,
        )


class FailingLedger:
    """账本替身：委托真实账本，但 `failing` 里的方法按注入方式「落盘失败」。

    用于模拟「发送成功后状态落盘失败」与「占位未命中」：`failed_calls` 记录被
    调用过的失败方法，供用例断言编排在落盘失败后没有继续评论段。

    `failure_mode`：
    - `"raise"`（默认）：抛 `LedgerUnavailableError`，模拟存储故障；
    - `"false"`：返回 `False`，模拟没有可更新的在途占位。
    """

    def __init__(
        self,
        delegate: Any,
        *,
        failing: tuple[str, ...],
        failure_mode: str = "raise",
    ) -> None:
        self._delegate = delegate
        self._failing = set(failing)
        self.failure_mode = failure_mode
        self.failed_calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name in self._failing:

            def failing_call(*args: Any, **kwargs: Any) -> bool:
                del args, kwargs
                self.failed_calls.append(name)
                if self.failure_mode == "raise":
                    raise LedgerUnavailableError("注入的状态库写入失败")
                return False

            return failing_call
        return getattr(self._delegate, name)


def components_of(plugin: Any) -> list[dict[str, Any]]:
    """读取插件注册的组件声明列表。"""

    return list(plugin.get_components())


def find_component(components: list[dict[str, Any]], component_type: str, name: str) -> dict[str, Any]:
    """按组件类型与名称取一条声明；不存在时直接断言失败并打印实际声明。"""

    for component in components:
        if component.get("type") == component_type and component.get("name") == name:
            return component
    raise AssertionError(f"未注册组件 type={component_type} name={name}；实际声明={components}")


def find_tool(plugin: Any, name: str = "forward_message") -> dict[str, Any]:
    """取指定 Tool 的组件声明。"""

    return find_component(components_of(plugin), "TOOL", name)


def find_hook(plugin: Any, hook: str) -> dict[str, Any]:
    """取订阅指定命名 Hook 的组件声明。"""

    for component in components_of(plugin):
        if component.get("type") != "HOOK_HANDLER":
            continue
        if (component.get("metadata") or {}).get("hook") == hook:
            return component
    raise AssertionError(f"未注册 Hook {hook}；实际声明={components_of(plugin)}")


def merged_forward_message(
    *,
    message_id: str = "m-forward-1",
    group_id: str = SOURCE_GROUP_ID,
    session_id: str = SOURCE_SESSION_ID,
    user_id: str = "90001",
    platform: str = SOURCE_PLATFORM,
    is_notify: bool = False,
    raw_message: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造宿主 `chat.receive.after_process` 载荷形态的合并转发消息。

    形态对齐第 1 组证据：序列化 SessionMessage（message_id / session_id /
    platform / message_info / raw_message / processed_plain_text），Hook 分发时以
    关键字 `message=` 传入（`bot.py:812-815` → `hook_payloads.serialize_session_message`）。
    **`is_notify` 是载荷/message 顶层键，不在 `message_info` 内**（宿主
    `message_utils.py:56` 的 `MessageDict`，写入 `:440`、读回 `:546-548`；`message_info`
    只有 user_info/group_info/additional_config，`:39-44`）。`additional_config` 使用
    宿主路由键工厂真实读写的别名（`route_key_factory.py:28-39`：账号
    `platform_io_account_id`，作用域 `platform_io_scope`；`"scope"` 不是宿主别名）。

    实现复用 `received_message`：同一个宿主载荷形状只保留一个构造点，夹具默认会话与
    来源装配（`prepare_source`）共用 `SOURCE_SESSION_ID`，不出现两套默认值。
    转发节点键使用宿主**接收侧**序列化名 `user_id` / `user_nickname` /
    `user_cardname` / `message_id` / `content`（MaiBot `src/plugin_runtime/host/message_utils.py:210-222`，
    反向解析 `:304-324`）——`nickname` / `segments` 只是 `send.forward` 入参别名
    （`src/plugin_runtime/capabilities/core.py:484-508`），不得混入本夹具。
    节点材料如何物化由消息网关决定（第 8 组联验），这里只提供一段可展开的转发节点。
    """

    payload = received_message(
        message_id=message_id,
        raw_message=(
            [
                forward_segment(
                    forward_node(
                        content=[text_segment("这是被转发的原文")],
                        message_id=f"{message_id}-node-1",
                        user_id=user_id,
                    )
                )
            ]
            if raw_message is None
            else raw_message
        ),
        session_id=session_id,
        group_id=group_id,
        user_id=user_id,
        platform=platform,
        is_notify=is_notify,
    )
    payload["message"]["processed_plain_text"] = "[合并转发]"
    return payload


def received_message(
    *,
    message_id: str,
    raw_message: list[dict[str, Any]],
    session_id: str = SOURCE_SESSION_ID,
    group_id: str = SOURCE_GROUP_ID,
    group_name: str = "测试源群",
    user_id: str = "90001",
    platform: str = SOURCE_PLATFORM,
    account_id: str = SOURCE_ACCOUNT_ID,
    scope: str = SOURCE_SCOPE,
    is_notify: bool = False,
) -> dict[str, Any]:
    """构造观察 Hook 的入站消息载荷（宿主 `serialize_session_message` 同形）。

    形状对齐第 1 组核实：`is_notify` 是 message 顶层键（`message_utils.py:56`，
    `message_info` 只含 user_info/group_info/additional_config，`:39-44`）；
    账号与作用域在 `additional_config` 且使用宿主别名（`route_key_factory.py:28-39`，
    入站由 `supervisor.py:1496-1501` 写入 `platform_io_account_id` / `platform_io_scope`）。
    """

    return {
        "message": {
            "message_id": message_id,
            "session_id": session_id,
            "platform": platform,
            "message_info": {
                "group_info": {"group_id": group_id, "group_name": group_name},
                "user_info": {"user_id": user_id, "user_nickname": "群友", "user_cardname": ""},
                "additional_config": {
                    HOST_ACCOUNT_ID_KEYS[0]: account_id,
                    HOST_SCOPE_KEYS[0]: scope,
                },
            },
            "raw_message": [dict(segment) for segment in raw_message],
            "is_notify": is_notify,
            "processed_plain_text": "入站消息",
        }
    }


def observe_hook_handler(plugin: Any) -> Any:
    """经注册组件取观察 Hook 的实际处理器（宿主分发同一入口）。"""

    hook = find_hook(plugin, RECEIVE_AFTER_PROCESS_HOOK)
    return getattr(plugin, hook["metadata"]["handler_name"])


async def drive_observe_hook(
    plugin: Any,
    clock: FakeClock,
    payload: dict[str, Any],
    *,
    until: Any = None,
    ticks: int = 200,
) -> None:
    """模拟宿主后台分发一次观察 Hook，并推进注入时钟直到工作结束。

    宿主对 observe 处理器是后台任务分发（`hook_dispatcher.py:632-662`），因此这里
    同样以任务方式驱动；每个 tick 让出事件循环一次并推进本次新登记的注入时钟等待
    （不真实等待）。`until` 成立或 tick 用尽即停止；处理器抛出的异常会在这里重新抛出。
    """

    task = asyncio.create_task(observe_hook_handler(plugin)(**payload))
    advanced = 0
    for _ in range(ticks):
        await asyncio.sleep(0)
        for seconds in clock.sleeps[advanced:]:
            clock.advance(seconds)
        advanced = len(clock.sleeps)
        if until is not None and until():
            break
    if task.done() and not task.cancelled():
        error = task.exception()
        if error is not None:
            raise error


async def wait_until(predicate: Any, *, attempts: int = 1000) -> None:
    """让出事件循环直到条件成立（不依赖真实时间）。"""

    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("等待条件超时")


async def apply_config_update(plugin: Any, config: dict[str, Any]) -> None:
    """模拟宿主配置热更新：先注入最新配置，再调用 `on_config_update`。

    宿主顺序（`runner_main.py:2273-2281`）：scope 为 `self` 时先
    `_apply_plugin_config`（经 `set_plugin_config` 归一注入，`:899-901`），再调用
    `on_config_update(scope, config_data, config_version)`。`config` 与宿主一样是
    归一后的完整配置（缺省字段回落模型默认值）。
    """

    plugin.set_plugin_config(config)
    await plugin.on_config_update("self", dict(config), "")


def expected_target_session_id(target_group_id: str = TARGET_GROUP_ID) -> str:
    """替身 `chat.open_session` 为「可信来源账号 + 目标群」派生的目标会话 ID。"""

    return FakeChatCapability._route_key(
        SOURCE_PLATFORM,
        target_group_id,
        SOURCE_ACCOUNT_ID,
        SOURCE_SCOPE,
    )


def ledger_key(
    *,
    message_id: str = "m-source-1",
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


def enabled_config(
    *,
    source_groups: tuple[str, ...] = (SOURCE_GROUP_ID,),
    target_groups: tuple[str, ...] = (TARGET_GROUP_ID,),
) -> dict[str, Any]:
    """构造「总开关开启 + 给定白名单」的插件配置覆盖值。"""

    return {
        "plugin": {"enabled": True},
        "scope": {"source_groups": list(source_groups), "target_groups": list(target_groups)},
    }


def source_stream(
    *,
    session_id: str = SOURCE_SESSION_ID,
    platform: str = SOURCE_PLATFORM,
    group_id: str = SOURCE_GROUP_ID,
    account_id: str = SOURCE_ACCOUNT_ID,
    scope: str = SOURCE_SCOPE,
    is_group_session: bool = True,
) -> dict[str, Any]:
    """构造宿主 `chat.get_all_streams` 返回的单条会话字段（`data.py:_serialize_stream`）。"""

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
    message_id: str = "m-source-1",
    session_id: str = SOURCE_SESSION_ID,
    group_id: str = SOURCE_GROUP_ID,
    user_id: str = "90001",
    raw_message: list[dict[str, Any]] | None = None,
    processed_plain_text: str = "源消息正文",
) -> dict[str, Any]:
    """构造宿主 `message.get_by_id` 返回形态的消息字典（`message_utils.py:412-450`）。"""

    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": SOURCE_PLATFORM,
        "message_info": {
            "group_info": {"group_id": group_id, "group_name": "测试源群"},
            "user_info": {"user_id": user_id, "user_nickname": "群友", "user_cardname": ""},
            "additional_config": {"account_id": SOURCE_ACCOUNT_ID, "platform_io_scope": SOURCE_SCOPE},
        },
        "raw_message": (
            [{"type": "text", "data": "源消息正文"}] if raw_message is None else list(raw_message)
        ),
        "processed_plain_text": processed_plain_text,
    }


def text_segment(text: str) -> dict[str, Any]:
    """文本段（宿主接收侧序列化形态）。"""

    return {"type": "text", "data": text}


def image_segment(
    *,
    description: str = "图片描述",
    binary_data_base64: str | None = "aW1hZ2UtYnl0ZXM=",
    binary_hash: str = "hash-image",
) -> dict[str, Any]:
    """图片段：`binary_data_base64` 为 None 时模拟「只有描述、媒体已丢失」。"""

    segment: dict[str, Any] = {"type": "image", "data": description, "hash": binary_hash}
    if binary_data_base64 is not None:
        segment["binary_data_base64"] = binary_data_base64
    return segment


def forward_segment(*nodes: dict[str, Any]) -> dict[str, Any]:
    """合并转发段（宿主接收侧序列化形态：`{"type": "forward", "data": [节点…]}`）。"""

    return {"type": "forward", "data": [dict(node) for node in nodes]}


def unexpanded_forward_segment(*, nodes: list[Any] | None = None) -> dict[str, Any]:
    """未展开的合并转发段：宿主没能恢复节点材料时收成 `DictComponent`
    （`message_utils.py:327-338`），序列化后是 `{"type": "dict", "data": {"type": "forward", …}}`。
    """

    return {"type": "dict", "data": {"type": "forward", "data": list(nodes or [])}}


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


def function_call_item(
    *,
    func_name: str,
    arguments: dict[str, Any],
    call_id: str = "call-1",
    item_id: str = "item-1",
) -> dict[str, Any]:
    """构造宿主 `maisaka.planner.after_response` 载荷里的单个函数调用 Item。

    形态对齐宿主快照序列化
    （`serialize_context_item_snapshot`：`item_type` / `meta` / `tool_call`
    `{call_id, func_name, args, extra_content}`）；宿主一次处理一批 tool_calls，
    同一载荷里可以混有指向其它工具的调用。
    """

    return {
        "item_type": "FunctionCallItem",
        "meta": {"item_id": item_id},
        "tool_call": {
            "call_id": call_id,
            "func_name": func_name,
            "args": dict(arguments),
            "extra_content": None,
        },
    }


def run_binding_hook(
    plugin: Any,
    *,
    session_id: str = SOURCE_SESSION_ID,
    output_items: Any,
) -> list[dict[str, Any]]:
    """模拟宿主调用 `maisaka.planner.after_response`，返回改写后的全部输出项。

    宿主形态：Hook 载荷由宿主构造（`session_id` + `output_items`），处理器可改写
    `output_items`；本轮改写结果就是随后 `plugin.invoke_tool` 收到的调用
    （`runner_main.py:2214` 取 `modified_kwargs`）。用例只经注册组件入口驱动，
    不直接调用私有函数。
    """

    hook = find_hook(plugin, PLANNER_AFTER_RESPONSE_HOOK)
    handler = getattr(plugin, hook["metadata"]["handler_name"])
    result = asyncio.run(
        handler(session_id=session_id, output_items=output_items, item_schema_version=1)
    )
    mutated_items = result["modified_kwargs"]["output_items"]
    if not isinstance(mutated_items, list) or not mutated_items:
        raise AssertionError(f"Hook 未返回可用的 output_items: {result!r}")
    return mutated_items


def issue_source_call(
    plugin: Any,
    *,
    session_id: str = SOURCE_SESSION_ID,
    message_id: str = "m-source-1",
    target_group_id: str = TARGET_GROUP_ID,
    comment: Any = "",
    extra_arguments: dict[str, Any] | None = None,
    output_items: Any = None,
) -> dict[str, Any]:
    """模拟宿主调用 `maisaka.planner.after_response`，返回该工具调用最终收到的参数。

    与 `run_binding_hook` 同一条注册组件入口；缺省只放一条指向 `forward_message`
    的调用，返回它的最终参数（供随后驱动 `plugin.forward_message`）。
    """

    arguments: dict[str, Any] = {
        "message_id": message_id,
        "target_group_id": target_group_id,
        "comment": comment,
    }
    arguments.update(extra_arguments or {})
    if output_items is None:
        output_items = [function_call_item(func_name="forward_message", arguments=arguments)]
    mutated_items = run_binding_hook(plugin, session_id=session_id, output_items=output_items)
    tool_call = mutated_items[0].get("tool_call")
    if not isinstance(tool_call, dict):
        raise AssertionError(f"output_items 缺少 tool_call: {mutated_items!r}")
    return dict(tool_call.get("args") or {})


def run_bound_forward(
    plugin: Any,
    *,
    session_id: str = SOURCE_SESSION_ID,
    message_id: str = "m-source-1",
    target_group_id: str = TARGET_GROUP_ID,
    comment: Any = "",
    extra_arguments: dict[str, Any] | None = None,
    clock: FakeClock | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """先经可信 Hook 绑定来源，再调用 `forward_message`；返回（工具结果, 工具参数）。

    传入 `clock` 时在事件循环内自动推进插件登记的等待（评论 0.5 秒延迟不真实等待），
    供不需要逐步断言等待时序的用例使用；需要断言时序的用例自己写事件循环场景。
    """

    arguments = issue_source_call(
        plugin,
        session_id=session_id,
        message_id=message_id,
        target_group_id=target_group_id,
        comment=comment,
        extra_arguments=extra_arguments,
    )
    if clock is None:
        result = asyncio.run(plugin.forward_message(**arguments))
    else:
        result = asyncio.run(run_with_clock(plugin, clock, arguments))
    return result, arguments


async def run_with_clock(plugin: Any, clock: FakeClock, arguments: dict[str, Any]) -> dict[str, Any]:
    """驱动一次工具调用并在其登记等待时推进注入时钟（不真实等待）。"""

    task = asyncio.create_task(plugin.forward_message(**arguments))
    advanced = 0
    for _ in range(1000):
        if task.done():
            return task.result()
        await asyncio.sleep(0)
        for seconds in clock.sleeps[advanced:]:
            clock.advance(seconds)
        advanced = len(clock.sleeps)
    raise AssertionError("工具调用在推进注入时钟后仍未结束")
