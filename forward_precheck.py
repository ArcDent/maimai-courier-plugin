"""转发请求的权限与内容预检（本地、不发送任何消息）。

工具入口只负责总开关与结果封装，本模块按固定顺序完成**本地**预检：

1. 三参数类型校验（ID 一律按字符串处理，禁止数值换算）；
2. 用一次性凭据换回可信 Planner 会话（拒绝模型伪造的会话/平台/授权字段）；
3. 校验来源为 QQ 群聊且属于某个转发分组，并按可信会话查询源消息归属与可读性；
4. 校验目标群与来源群同属一个转发分组、不同于源群（目标平台与账号可用性由直连准备阶段核对）；
5. 构建材料摘要：普通消息继续做完整内容预检（含提及文本化与不支持段拒绝），
   **合并转发卡片只取标识与归属核验**（顶层唯一形状，混排拒绝）：与客户端
   `SnowLumaClient.prepare_source` 的判定口径一致，不在本地重复构造卡片材料
   （卡片发送材料由客户端按源消息卡片标识展开查询构造，平台按目标群上下文重建）。

任一步失败都在产生任何发送副作用前整条拒绝，拒绝原因统一经 `Rejected.not_sent`
补「本次未发送任何消息」定尾；全部通过时返回 `PreparedForward`，由客户端投递编排
（`forward_delivery.py`）交给直连客户端执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .send_material import SendMaterial, UnsupportedContent, build_send_material, classify_top_level_shape
from .shared.identifiers import verbatim_identifier
from .shared.send_steps import SourceKind
from .source_binding import (
    ForwardArguments,
    Rejected,
    SourceBindingTable,
    parse_forward_arguments,
)

QQ_PLATFORM = "qq"
"""宿主 QQ 平台名：来源会话必须属于该平台。"""

ALL_PLATFORMS = "all_platforms"
"""宿主 `chat.get_all_streams` 的全平台取值。"""


@dataclass(frozen=True, slots=True)
class SourceSession:
    """可信来源会话：平台、账号、作用域与群号全部来自宿主会话表。"""

    session_id: str
    platform: str
    group_id: str
    account_id: str
    scope: str


@dataclass(frozen=True, slots=True)
class MaterialDigest:
    """材料结构摘要：只含种类与结构指纹（不含正文），满足结果渲染的结构面。

    普通消息直接用 `send_material.SendMaterial`；合并转发卡片只记发送路径种类
    （`SourceKind` 的 `forward`，与客户端 `prepare_source` 的判定口径一致）与结构
    指纹，避免在本地递归展开节点或重复构造卡片材料（卡片发送材料由客户端按源
    消息卡片标识展开查询构造）。
    """
    kind: SourceKind
    shape: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedForward:
    """预检通过的可发送计划（本地不发送）。

    `material` 只用于工具结果的 `send_kind` / `material_shape` 展示；资源标识不离开
    插件，卡片发送材料由客户端按展开查询构造。
    """

    arguments: ForwardArguments
    source: SourceSession
    material: SendMaterial | MaterialDigest
    onebot_message_id: str = ""


@dataclass(frozen=True, slots=True)
class _StreamFields:
    """宿主会话字典的归一字段（来源会话解析用）。"""

    session_id: str
    platform: str
    group_id: str
    account_id: str
    scope: str


def _stream_fields(stream: Mapping[str, Any]) -> _StreamFields:
    """按同一口径取宿主会话字典字段：字符串化并去首尾空白。"""

    def text(key: str) -> str:
        return str(stream.get(key) or "").strip()

    return _StreamFields(
        session_id=text("session_id"),
        platform=text("platform"),
        group_id=text("group_id"),
        account_id=text("account_id"),
        scope=text("scope"),
    )


async def precheck_forward_request(
    *,
    ctx: Any,
    config: Any,
    bindings: SourceBindingTable,
    arguments: Mapping[str, Any],
) -> PreparedForward | Rejected:
    """按顺序完成本地权限与内容预检；全部通过时返回 `PreparedForward`。

    任一步失败都在产生发送副作用前整条拒绝；本函数不发送任何消息，也不访问任何
    网络服务。
    """

    parsed = parse_forward_arguments(arguments)
    if isinstance(parsed, Rejected):
        return parsed

    # 可信来源只由一次性的 Hook 凭据决定：工具调用参数里的会话/平台/账号/授权
    # 字段一概不读（模型可伪造，见模块 docstring）。
    source_session_id = bindings.consume(parsed.message_id, arguments)
    if source_session_id is None:
        return Rejected.not_sent(
            "无法确认本次调用的可信来源（不接受模型提供的会话或授权字段）"
        )

    source = await _resolve_source_session(ctx, source_session_id)
    if isinstance(source, Rejected):
        return source
    if not source_group_is_allowed(config, source.group_id):
        return Rejected.not_sent("调用来源群不在任何转发分组内")

    message = await _fetch_source_message(ctx, parsed.message_id, source.session_id)
    if isinstance(message, Rejected):
        return message

    if parsed.target_group_id == source.group_id:
        return Rejected.not_sent("目标群不能是调用来源群自身")
    if shared_forward_group(config, source.group_id, parsed.target_group_id) is None:
        return Rejected.not_sent("目标群不在与来源群相同的转发分组内")

    onebot_message_id = _mapped_source_id(message, parsed.message_id, source)
    if not onebot_message_id:
        return Rejected.not_sent("源消息归属或 OneBot 入站对应信息不足，无法确认源消息标识")

    material = _material_digest(message.get("raw_message"))
    if isinstance(material, Rejected):
        return material

    return PreparedForward(arguments=parsed, source=source, material=material, onebot_message_id=onebot_message_id)


def _mapped_source_id(message: Mapping[str, Any], message_id: str, source: SourceSession) -> str:
    """按宿主保留链核对源消息归属；格式合法本身从不授予身份。"""
    if (not verbatim_identifier(message_id) or message.get("message_id") != message_id
            or message.get("session_id") != source.session_id or message.get("platform") != "qq"):
        return ""
    info = message.get("message_info")
    if not isinstance(info, Mapping):
        return ""
    group = info.get("group_info")
    metadata = info.get("additional_config")
    if not isinstance(group, Mapping) or not isinstance(metadata, Mapping):
        return ""
    # 比较平台整数时只转十进制文本；查询 ID 与账本键均不重编码。
    def identity(value: object) -> str:
        return verbatim_identifier(str(value)) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if (identity(group.get("group_id")) != source.group_id
            or identity(metadata.get("self_id")) != source.account_id
            or metadata.get("napcat_message_type") != "group"
            or not verbatim_identifier(metadata.get("client_type"))):
        return ""
    if any(key in metadata and identity(metadata[key]) != source.account_id
           for key in ("platform_io_account_id", "account_id", "bot_account")):
        return ""
    return message_id


def _material_digest(raw_message: Any) -> SendMaterial | MaterialDigest | Rejected:
    """构建材料摘要：卡片只取标识与归属核验，普通消息仍做完整内容预检。

    卡片只检查**顶层**形态：唯一一张卡片（含宿主未展开的 `dict` 包装）走「标识 +
    归属」核验，与客户端 `SnowLumaClient.prepare_source` 的判定口径一致（卡片发送
    材料由客户端按源消息卡片标识展开查询构造，本地不重复构造）；但卡片与其它顶层
    内容混排无法保持卡片形态，在本地即整条拒绝（顶层唯一判定是既有规则，不是递归
    校验）。
    """

    if not isinstance(raw_message, list) or not raw_message:
        return Rejected.not_sent("源消息没有可发送的内容段")
    shape = classify_top_level_shape(raw_message)
    if shape == "mixed":
        return Rejected.not_sent(
            "合并转发卡片与其它顶层内容混排，无法保持卡片形态，整条拒绝"
        )
    if shape == "forward":
        # 卡片只取标识与归属核验（客户端 prepare 同口径）：kind 走共享发送路径
        # 三值，shape 只表示「一张卡片」，不持有资源 ID、不展开内部节点。
        return MaterialDigest(kind="forward", shape=("forward",))
    material = build_send_material(raw_message)
    if isinstance(material, UnsupportedContent):
        return Rejected.not_sent(material.reason)
    return material


async def _resolve_source_session(ctx: Any, session_id: str) -> SourceSession | Rejected:
    """按可信会话 ID 在宿主会话表里解析平台、账号、作用域与群号。

    SDK 已把 `chat.get_all_streams` 归一为会话列表；宿主异常在 Runner 侧表现为
    异常。
    """

    try:
        streams = await ctx.chat.get_all_streams(platform=ALL_PLATFORMS)
    except Exception:
        return Rejected.not_sent("无法验证本次调用的来源会话")
    if not isinstance(streams, list):
        return Rejected.not_sent("无法验证本次调用的来源会话")

    stream: dict[str, Any] | None = None
    for item in streams:
        if isinstance(item, dict) and str(item.get("session_id") or "").strip() == session_id:
            stream = item
            break
    if stream is None:
        return Rejected.not_sent("无法确认本次调用的可信来源会话")

    fields = _stream_fields(stream)
    if not stream.get("is_group_session") or not fields.group_id:
        return Rejected.not_sent("本次调用来源不是群聊，私聊不支持跨群转发")
    if fields.platform != QQ_PLATFORM:
        return Rejected.not_sent("本次调用来源不在 QQ 群聊范围内")
    if not fields.account_id:
        return Rejected.not_sent("来源会话缺少可确认的机器人账号")

    return SourceSession(
        session_id=session_id,
        platform=fields.platform,
        group_id=fields.group_id,
        account_id=fields.account_id,
        scope=fields.scope,
    )


async def _fetch_source_message(
    ctx: Any,
    message_id: str,
    session_id: str,
) -> dict[str, Any] | Rejected:
    """按可信会话限定查询源消息，验证它确实属于本次调用的来源群。

    SDK 已把 `message.get_by_id` 归一为单条消息字典或 `None`。
    """

    try:
        message = await ctx.message.get_by_id(
            message_id,
            chat_id=session_id,
            include_binary_data=True,
        )
    except Exception:
        return Rejected.not_sent("无法读取源消息")
    if not isinstance(message, dict) or message.get("success") is False:
        return Rejected.not_sent("源消息不可定位或不属于本次调用的源群")

    returned_session_id = str(message.get("session_id") or "").strip()
    if returned_session_id and returned_session_id != session_id:
        return Rejected.not_sent("源消息归属与本次调用的可信来源不一致")
    return message


def group_id_entries(values: Any) -> tuple[str, ...]:
    """把配置里的群号序列归一成去空白字符串（字符串化、丢弃空值、保持配置顺序）。

    工具侧预检、成对判定、阶段复验与自动入口共用本口径：用户可能手工编辑配置文件，
    判定不得因为一处实现只接受 `str` 而在另一处接受字符串化结果。
    """

    try:
        items = list(values)
    except TypeError:
        return ()
    return tuple(text for item in items if (text := str(item).strip()))


@dataclass(frozen=True, slots=True)
class ForwardGroup:
    """一个转发分组的归一视图（纯数据：分组名 + 源群号 + 目标群号）。

    `name` 只作用户标识与日志前缀（压平后），不参与任何判定；群号已经
    `group_id_entries` 归一，源/目标集合分别只由成对判定、源群判定与自动入口列举
    消费。
    """

    name: str
    source_groups: tuple[str, ...]
    target_groups: tuple[str, ...]


def forward_group_entries(config: Any) -> tuple[ForwardGroup, ...]:
    """按配置顺序归一全部分组：`name` 去首尾空白，群号复用 `group_id_entries` 口径。

    纯函数：不查询、不发送、不修改输入，随处可调。分组判定族（成对判定、源群判定）
    与自动入口的目标列举都以本视图为唯一归一来源，避免各自复制一份群号口径。
    """

    return tuple(
        ForwardGroup(
            name=str(group.name).strip(),
            source_groups=group_id_entries(group.source_groups),
            target_groups=group_id_entries(group.target_groups),
        )
        for group in config.scope.groups
    )


def source_group_is_allowed(config: Any, group_id: str) -> bool:
    """来源群是否出现在任一分组的源群集合内（本地预检、自动入口筛选与自动入口重验共用）。"""

    return forward_group_of(config, group_id) is not None


def forward_group_of(config: Any, source_group_id: str) -> ForwardGroup | None:
    """来源群所属转发分组（群号归一后匹配），否则 `None`。

    「来源群号归一 → 所属分组」的成员判定只有这一处：成对判定与自动入口的同组目标
    列举都以本函数定位来源群所属分组，再在该分组内判目标。
    """

    source = str(source_group_id).strip()
    for entry in forward_group_entries(config):
        if source in entry.source_groups:
            return entry
    return None


def shared_forward_group(config: Any, source_group_id: str, target_group_id: str) -> str | None:
    """来源群与目标群是否属于同一转发分组：命中返回该分组名，否则 `None`。

    成对判定只有这一处定义：来源群号与目标群号一起进入，签名上不存在「只判目标」的
    用法，从形式上杜绝漏绑来源的越权判定。命中条件是「来源群在该组源群集合内，且
    目标群在该组目标集合内」；目标群可跨组重复，来源群只能属于一个分组，因此命中时
    至多只有一个分组符合。「目标 = 来源群自身」由调用方（工具预检）单独拒绝。
    """

    entry = forward_group_of(config, source_group_id)
    if entry is None:
        return None
    target = str(target_group_id).strip()
    return entry.name if target in entry.target_groups else None
