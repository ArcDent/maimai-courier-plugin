"""内存平台装配：真实预检、真实账本与真实客户端，只有平台动作是合成替身。

服务不触网的默认档测试文件（`tests/test_auto_entry.py`、`test_lifecycle.py`、
`test_plugin.py`）；其消息段与装配构造器（`received_message` / `onebot_segments` /
`at_segment` 等）也供真实回环 HTTP 验收 `tests/test_direct_acceptance.py` 复用，
两者不互相冒充：本模块不含任何 HTTP 服务端，也不用内存传输替代真实线路断言。
默认 pytest 档（不装 `aiohttp`）可完整导入本模块。

装配口径与现行用例一致：账本用真实 SQLite 账本（`tests.support.open_test_ledger`），
传输替身复用 `tests/snowluma_client_support.MemoryOneBotTransport`（`OneBotTransport`
协议八动作 + `close`，好友名册与私聊路由参数由基类单点提供），平台源记录由宿主原始段
经 `onebot_segments` 冻结映射得到。
共享替身（可控时钟、几何常量、账本键、配置与工厂注入点、公共消息样本）一律取自
`tests.support`，本模块只保留平台装配特有的部分。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from maimai_courier_plugin.shared.delivery import LedgerUnavailableError
from maimai_courier_plugin.delivery_ledger import DeliveryLedger
from maimai_courier_plugin.plugin import RECEIVE_AFTER_PROCESS_HOOK
from tests.snowluma_client_support import MemoryOneBotTransport
from tests.support import (
    HTTP_URL,
    MESSAGE_ID,
    OTHER_TARGET_GROUP_ID,
    PRIVATE_USER_ID,  # 单一来源在 tests.support；经本模块转发给 test_lifecycle 的既有导入路径
    SOURCE_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    FakeClock,
    enabled_config,
    envelope,
    find_hook,
    forward_node,
    forward_segment,
    issue_source_call,
    ledger_key,
    make_plugin,
    open_test_ledger,
    source_message,
    source_record,
    source_stream,
    text_segment,
)

DEFAULT_RESOURCE_ID = "res-card-synthetic"
"""卡片段自带的资源引用：只作 `get_forward_msg` 展开查询的卡片标识，发送载荷不得提交它。"""

DEFAULT_MEDIA_URL = "https://cdn.example.invalid/media.png"
"""普通图片的合成媒体地址（只出现在平台侧源记录，不进入工具参数）。"""

ACCEPTANCE_TOKEN = "synthetic-acceptance-only"
"""回环端点的合成令牌（只经认证头，不得进 URL、日志、账本或工具结果）。"""

SHORT_WAIT_SECONDS = 0.5
"""驱动辅助只推进的短等待上限（秒）：与生产就绪轮询间隔同口径的来源。

口径来源是生产常量 `auto_entry.AUTO_READINESS_POLL_INTERVAL_SECONDS`（自动入口就绪
轮询间隔，0.5 秒）：`drive_observe_hook` 与 `run_with_clock` 只把不超过该值的注入
时钟等待当成立即可推进的就绪轮询间隔；动作级单动作预算（预搜索与工具路径共用的
10 秒）不在这里推进，避免把内存平台上的真实只读查询判成超时放行。
"""


def media_url_for(ordinal: int) -> str:
    """按序号派生合成媒体地址（与 `onebot_segments` 的平台侧映射一致）。"""

    return DEFAULT_MEDIA_URL if ordinal == 1 else DEFAULT_MEDIA_URL.replace(".png", f"-{ordinal}.png")


def onebot_segments(raw_message: list[dict[str, Any]], *, resource_id: str = DEFAULT_RESOURCE_ID,
                    media_url: str | None = None) -> list[dict[str, Any]]:
    """宿主接收侧原始段 → OneBot `get_msg` 记录 `message` 段的冻结映射。

    预期输出仍由各用例写独立字面量：这里只把两种输入格式对齐（文字/图片/提及/
    合并转发，含宿主未展开的 `dict` 包装形态），不替用例声明预期。
    """

    result: list[dict[str, Any]] = []
    ordinal = 0
    for segment in raw_message:
        kind, data = segment.get("type"), segment.get("data")
        if kind == "text":
            result.append({"type": "text", "data": {"text": data}})
        elif kind == "image":
            ordinal += 1
            result.append({"type": "image", "data": {"url": media_url or media_url_for(ordinal)}})
        elif kind == "at":
            fields: dict[str, Any] = {"qq": data.get("target_user_id")}
            if data.get("target_user_cardname"):
                fields["card"] = data["target_user_cardname"]
            elif data.get("target_user_nickname"):
                fields["nickname"] = data["target_user_nickname"]
            result.append({"type": "at", "data": fields})
        elif kind == "forward" or (kind == "dict" and isinstance(data, dict) and data.get("type") == "forward"):
            result.append({"type": "forward", "data": {"id": resource_id}})
        else:
            result.append(deepcopy(segment))
    return result


class Gates:
    """测试控制公开动作的等待位置，生产不认识这些标签。"""

    def __init__(self) -> None:
        self.events: dict[str, asyncio.Event] = {}
        self.arrivals: dict[str, int] = {}

    def pause(self, stage: str) -> None:
        self.events[stage] = asyncio.Event()

    def release(self, stage: str) -> None:
        self.events[stage].set()

    async def arrive(self, stage: str) -> None:
        self.arrivals[stage] = self.arrivals.get(stage, 0) + 1
        if stage in self.events:
            await self.events[stage].wait()


_SEND_ACTIONS = frozenset({"send_msg", "send_group_forward_msg", "send_forward_msg"})
"""固定发送动作表：卡片展开节点重组投递。"""


class DirectOneBot(MemoryOneBotTransport):
    """内存传输替身 + 公开动作等待点与提交断言助手：不断言客户端内部状态。"""

    def __init__(self) -> None:
        super().__init__()
        self.gates = Gates()
        self.expand_failure = False
        """展开查询失败开关：置真时 `get_forward_msg` 按业务失败信封应答。

        预搜索 fail-open 的注入点：命中时只读查询视为「平台确认该卡片不存在或不可
        展开」，`card_content_rejection` 按查询失败放行，自动判断照常进行。
        """

    async def _reply(self, action: str, params: Any) -> dict[str, Any]:
        if action in _SEND_ACTIONS:
            await self.gates.arrive("platform_commit")
        if self.expand_failure and action == "get_forward_msg":
            return envelope(None, status="failed", retcode=100, message="not found")
        return await super()._reply(action, params)

    @property
    def send_count(self) -> int:
        return len(self.sent)

    def sent_actions(self) -> list[str]:
        return [call["action"] for call in self.sent]

    def sent_segments(self) -> list[Any]:
        """按动作取本次提交的内容段（文字单节点卡片取节点 content）。"""

        segments: list[Any] = []
        for call in self.sent:
            params = call["params"]
            if call["action"] == "send_forward_msg":
                segments.extend(node["data"]["content"] for node in params["messages"])
            elif "message" in params:
                segments.append(params["message"])
        return segments

    def sent_texts(self) -> list[str]:
        return [
            segment["data"]["text"]
            for segments in self.sent_segments()
            for segment in segments
            if isinstance(segment, dict) and segment.get("type") == "text"
            and isinstance(segment.get("data"), dict) and isinstance(segment["data"].get("text"), str)
        ]


class FaultLedger:
    """公开存储依赖注入：按操作名注入一次存储故障，并登记关闭调用；断言仍读真实账本。"""

    def __init__(self, ledger: DeliveryLedger) -> None:
        self.ledger = ledger
        self.faults: set[str] = set()
        self.closed: list[str] = []

    def fail_next(self, operation: str) -> None:
        self.faults.add(operation)

    def close(self) -> None:
        self.closed.append("close")
        self.ledger.close()

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.ledger, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if name in self.faults:
                self.faults.remove(name)
                raise LedgerUnavailableError("模拟存储失败")
            return method(*args, **kwargs)

        return call


def store_source_message(onebot: MemoryOneBotTransport, *, message_id: str,
                         group_id: str = SOURCE_GROUP_ID, message: list[dict[str, Any]] | None = None,
                         user_id: str = "90001", **kwargs: Any) -> dict[str, Any]:
    """登记平台侧源消息记录（`get_msg` 的应答）：`sender` 只用于文字节点署名。"""

    stored = {
        "message_id": message_id, "message_type": "group", "group_id": group_id,
        "user_id": user_id, "message": message or [],
        "sender": {"user_id": user_id, "nickname": "群友", "card": ""},
    }
    stored.update(kwargs)
    onebot.source = stored
    return stored


def prepare_source(
    ctx: Any,
    *,
    raw_message: list[dict[str, Any]] | None = None,
    message_id: str = MESSAGE_ID,
    **stream_overrides: Any,
) -> None:
    """装配一条可信来源会话与其源消息（宿主查询面替身）。"""

    ctx.chat.streams = [source_stream(**stream_overrides)]
    ctx.message.store = {
        (SOURCE_SESSION_ID, message_id): source_message(message_id=message_id, raw_message=raw_message),
    }


def at_segment(
    target_user_id: Any = "30001",
    *,
    nickname: Any = None,
    cardname: Any = None,
) -> dict[str, Any]:
    """提及段（宿主接收侧序列化形态：标识 + 可选昵称/群名片）。"""

    return {
        "type": "at",
        "data": {
            "target_user_id": target_user_id,
            "target_user_nickname": nickname,
            "target_user_cardname": cardname,
        },
    }


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


def unexpanded_forward_segment(*, nodes: list[Any] | None = None) -> dict[str, Any]:
    """未展开的合并转发段：宿主没能恢复节点材料时收成 `DictComponent`。"""

    return {"type": "dict", "data": {"type": "forward", "data": list(nodes or [])}}


def received_message(
    *,
    message_id: str,
    raw_message: list[dict[str, Any]],
    session_id: str = SOURCE_SESSION_ID,
    group_id: str = SOURCE_GROUP_ID,
    group_name: str = "测试源群",
    user_id: str = "90001",
    platform: str = "qq",
    account_id: str = SOURCE_ACCOUNT_ID,
    scope: str = "qq",
    is_notify: bool = False,
) -> dict[str, Any]:
    """观察 Hook 的入站消息载荷（宿主 `serialize_session_message` 同形）。

    `is_notify` 是 message 顶层键，不在 `message_info` 内；账号与作用域在
    `additional_config` 且使用宿主路由键工厂别名。
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
                    "platform_io_account_id": account_id,
                    "platform_io_scope": scope,
                },
            },
            "raw_message": [dict(segment) for segment in raw_message],
            "is_notify": is_notify,
            "processed_plain_text": "入站消息",
        }
    }


def merged_forward_message(
    *,
    message_id: str = "m-forward-1",
    group_id: str = SOURCE_GROUP_ID,
    session_id: str = SOURCE_SESSION_ID,
    user_id: str = "90001",
    platform: str = "qq",
    is_notify: bool = False,
    raw_message: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`chat.receive.after_process` 载荷形态的合并转发消息（可与工具调用共用）。"""

    payload = received_message(
        message_id=message_id,
        raw_message=(
            [forward_segment(forward_node(content=[text_segment("这是被转发的原文")],
                                         message_id=f"{message_id}-node-1", user_id=user_id))]
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


def observe_hook_handler(plugin: Any) -> Any:
    """经注册组件取观察 Hook 的实际处理器。"""

    hook = find_hook(plugin, RECEIVE_AFTER_PROCESS_HOOK)
    return getattr(plugin, hook["metadata"]["handler_name"])


def wake_once(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    source_raw_message: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    onebot: Any = None,
    until: Any = None,
    ticks: int = 200,
) -> tuple[Any, Any]:
    """一条候选从装载到驱动观察 Hook 的最小链路（公开工厂 + 注册 Hook）。

    传输替身经公开工厂注入（单测零真实联网）：默认现场构造内存 `DirectOneBot`，并把
    平台侧源消息播种为一张合并转发卡片（`message_id` 取本次载荷的消息 ID，与宿主侧
    样本同一标识，供自动路径的只读预搜索命中展开查询）；传入 `onebot=` 时工厂直接复用
    该实例（供调用方预置 `expanded` / `expand_failure`）。
    """

    clock = FakeClock()
    message_id = payload["message"]["message_id"]
    if onebot is None:
        onebot = DirectOneBot()
        onebot.source = source_record(
            message=[{"type": "forward", "data": {"id": DEFAULT_RESOURCE_ID}}],
            message_id=message_id)
    plugin, ctx = make_plugin(
        tmp_path, clock=clock, config=config or enabled_config(),
        transport_factory=lambda _connection: onebot,
    )
    if source_raw_message is None:
        prepare_source(ctx, message_id=message_id)
    else:
        prepare_source(ctx, message_id=message_id, raw_message=source_raw_message)
    asyncio.run(drive_observe_hook(plugin, clock, payload, until=until, ticks=ticks))
    return plugin, ctx


async def drive_observe_hook(
    plugin: Any,
    clock: Any,
    payload: dict[str, Any],
    *,
    until: Any = None,
    ticks: int = 200,
) -> None:
    """模拟宿主后台分发一次观察 Hook，并推进注入时钟直到工作结束。

    宿主对 observe 处理器是后台任务分发，因此这里同样以任务方式驱动；每个 tick
    让出事件循环一次并推进本次新登记的**短等待**（≤0.5 秒的就绪轮询间隔），不真实
    等待。动作级的单动作预算（10 秒，预搜索与工具路径共用）不在这里推进：内存平台
    上的只读查询应当真实完成，推进预算只会把查询判成超时放行，并让去重时间窗被虚拟
    时间跳过（与 `run_with_clock` 同一口径）。
    """

    task = asyncio.create_task(observe_hook_handler(plugin)(**payload))
    advanced = 0
    for _ in range(ticks):
        await asyncio.sleep(0)
        for seconds in clock.sleeps[advanced:]:
            if seconds <= SHORT_WAIT_SECONDS:
                clock.advance(seconds)
        advanced = len(clock.sleeps)
        if until is not None and until():
            break
    if task.done() and not task.cancelled():
        error = task.exception()
        if error is not None:
            raise error


async def apply_config_update(plugin: Any, config: dict[str, Any]) -> None:
    """模拟宿主配置热更新：先注入最新配置，再调用 `on_config_update`。"""

    plugin.set_plugin_config(config)
    await plugin.on_config_update("self", dict(config), "")


async def run_with_clock(plugin: Any, clock: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """让即时查询先完成，仅推进真正的短等待（如评论 0.5 秒延迟），不真实等待。"""

    task = asyncio.create_task(plugin.forward_message(**arguments))
    advanced = len(clock.sleeps)
    for index in range(5000):
        if task.done():
            return task.result()
        await asyncio.sleep(0)
        if index % 50 == 49:
            for seconds in clock.sleeps[advanced:]:
                if seconds <= SHORT_WAIT_SECONDS:
                    clock.advance(seconds)
            advanced = len(clock.sleeps)
    raise AssertionError("工具调用在推进注入时钟后仍未结束")


def run_bound_forward(
    plugin: Any,
    *,
    session_id: str = SOURCE_SESSION_ID,
    message_id: str = MESSAGE_ID,
    target_group_id: str = TARGET_GROUP_ID,
    comment: Any = "",
    extra_arguments: dict[str, Any] | None = None,
    clock: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """先经可信 Hook 绑定来源，再调用 `forward_message`；返回（工具结果, 工具参数）。"""

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


def intent_criteria_region(intent: str) -> str:
    """取判断意图里「用户配置的自动分享标准」区域之后的标准原文（最后一段）。"""

    lines = intent.split("\n")
    for index, line in enumerate(lines):
        if "用户配置的自动分享标准" in line:
            return "\n".join(lines[index + 1:])
    raise AssertionError(f"意图缺少用户配置的自动分享标准区域: {intent!r}")


class DirectFixture:
    """公开装配替身：真实预检、真实客户端与账本，仅平台动作走内存传输。

    首个连接复用已播种的传输替身（单连接装配的断言语义不变）；连接替换（端点或
    签名变化）时才为新连接构造独立传输会话，可用 `stage_next_source` 预置其源
    消息。`constructed` 登记组合根交给工厂的连接，`close_calls` 经存储替身登记
    账本关闭，供卸载预算用例断言。
    """

    def __init__(self, path: Path, *, raw_message: list[dict[str, Any]] | None = None,
                 target_groups: tuple[str, ...] = (TARGET_GROUP_ID,),
                 source_groups: tuple[str, ...] = (SOURCE_GROUP_ID,),
                 groups: list[dict[str, Any]] | None = None,
                 message_types: tuple[str, ...] = ("forward",),
                 share_criteria: str | None = None, clock: Any = None,
                 config_overrides: dict[str, dict[str, Any]] | None = None,
                 http_url: str = HTTP_URL, onebot_raw_message: list[dict[str, Any]] | None = None) -> None:
        self.data_dir = path
        path.mkdir(parents=True, exist_ok=True)
        self.clock = clock or FakeClock()
        self.ledger = open_test_ledger(path, clock=self.clock)
        self.storage = FaultLedger(self.ledger)
        self.close_calls = self.storage.closed
        config = enabled_config(source_groups=source_groups, target_groups=target_groups,
                                groups=groups, message_types=message_types,
                                share_criteria=share_criteria, http_url=http_url)
        for section, values in (config_overrides or {}).items():
            config.setdefault(section, {}).update(values)
        self.onebot = DirectOneBot()
        self.onebot.source = source_record(
            message=[{"type": "forward", "data": {"id": DEFAULT_RESOURCE_ID}}],
            message_id=MESSAGE_ID)
        self.transports: list[MemoryOneBotTransport] = [self.onebot]
        self.constructed: list[Any] = []
        self._pending_source: dict[str, Any] | None = None
        self.plugin, self.ctx = make_plugin(
            path, config=config, clock=self.clock,
            transport_factory=self._factory,
            environ={}, ledger_opener=lambda _: self.storage,
        )
        self.raw_message = raw_message
        self.onebot_raw_message = onebot_raw_message
        if raw_message is not None:
            self.prepare_source(message_id=MESSAGE_ID, raw_message=raw_message,
                                onebot_raw_message=onebot_raw_message)

    @property
    def transport(self) -> MemoryOneBotTransport:
        """首个连接上的传输替身（单连接装配下即唯一平台动作替身）。"""

        return self.onebot

    def _factory(self, connection: Any) -> Any:
        self.constructed.append(connection)
        if len(self.constructed) == 1:
            # 首个连接复用已播种的传输：既有单次投递用例的断言语义不变。
            return self.onebot
        # 连接替换（端点／签名变化）才走到这里：为新连接构造一条独立传输会话。
        # 默认沿用同一张合并转发卡片样本；单次投递为新端点预置源消息标识时用
        # `stage_next_source`（供「旧客户端关闭、新客户端独立」用例区分新旧会话）。
        transport = MemoryOneBotTransport()
        if self._pending_source is not None:
            transport.source = self._pending_source
            self._pending_source = None
        self.transports.append(transport)
        return transport

    def prepare_source(self, *, message_id: str = MESSAGE_ID,
                       raw_message: list[dict[str, Any]] | None = None,
                       onebot_raw_message: list[dict[str, Any]] | None = None,
                       **stream_overrides: Any) -> None:
        """同时装配宿主侧可信来源与平台侧同账号源消息（两边同一标识）。

        默认样本是合并转发卡片（与组合根回归的卡片投递一致）；`raw_message` 显式
        给定文字/图片/混排时改用给定段。
        """

        host_raw = raw_message if raw_message is not None else [forward_segment(forward_node())]
        prepare_source(self.ctx, message_id=message_id, raw_message=host_raw, **stream_overrides)
        store_source_message(
            self.onebot, message_id=message_id,
            group_id=stream_overrides.get("group_id", SOURCE_GROUP_ID),
            message=onebot_segments(onebot_raw_message if onebot_raw_message is not None else host_raw),
        )

    def stage_next_source(self, *, message_id: str) -> None:
        """预置下一次连接替换所构造传输的源消息：与宿主侧同一张合并转发卡片。"""

        self._pending_source = source_record(
            message=[{"type": "forward", "data": {"id": DEFAULT_RESOURCE_ID}}],
            message_id=message_id)

    def record(self, *, message_id: str = MESSAGE_ID,
               target_group_id: str = TARGET_GROUP_ID) -> Any:
        """读真实账本事实（字典快照）；插件关闭账本后重开一份再读（不复制投递状态）。"""

        try:
            record = self.storage.get(ledger_key(message_id=message_id, target_group_id=target_group_id))
        except LedgerUnavailableError:
            reopened = open_test_ledger(self.data_dir, clock=self.clock)
            try:
                record = reopened.get(ledger_key(message_id=message_id, target_group_id=target_group_id))
            finally:
                reopened.close()
        return None if record is None else asdict(record)

    def sent_segments(self) -> list[Any]:
        """本次各发送动作提交的内容段（文字单节点卡片取节点 content）。"""

        return self.onebot.sent_segments()

    def sent_texts(self) -> list[str]:
        return self.onebot.sent_texts()

    def close(self) -> None:
        asyncio.run(self.plugin.on_unload())
        self.ledger.close()


def make_direct_plugin(path: Path, **kwargs: Any) -> DirectFixture:
    return DirectFixture(path, **kwargs)


def make_auto_plugin(
    tmp_path: Path,
    *,
    clock: Any = None,
    config: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """自动入口用例的本地装载助手：与 `make_plugin` 同签名面，注入内存传输替身。

    合并转发候选就绪后的只读预搜索（`get_msg` / `get_forward_msg`）经 `DirectOneBot`
    内存平台完成，预搜索只读查询落内存平台、不触真实网络；未播种的源消息 ID 落在
    「非卡片」分支直接放行，与真实连接失败时的 fail-open 行为一致。`config` 缺省
    `enabled_config()`。需要特定展开结果（命中嵌套内层卡片、展开查询失败等）的用例
    用 `make_direct_plugin`（`DirectFixture` 可预置 `expanded` / `expand_failure`）
    或直接调 `run_auto_entry` 注入 `presearch`，不走本助手。
    """

    return make_plugin(
        tmp_path,
        clock=clock,
        config=enabled_config() if config is None else config,
        transport_factory=lambda _connection: DirectOneBot(),
    )


__all__ = [
    "ACCEPTANCE_TOKEN",
    "DEFAULT_MEDIA_URL",
    "DEFAULT_RESOURCE_ID",
    "DirectFixture",
    "FaultLedger",
    "apply_config_update",
    "at_segment",
    "drive_observe_hook",
    "forward_node",
    "image_segment",
    "intent_criteria_region",
    "make_auto_plugin",
    "make_direct_plugin",
    "merged_forward_message",
    "onebot_segments",
    "prepare_source",
    "received_message",
    "run_bound_forward",
    "store_source_message",
    "unexpanded_forward_segment",
]
