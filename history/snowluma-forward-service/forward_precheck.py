"""转发请求的权限与内容预检（本地、不发送任何消息，design D2/D9）。

工具入口只负责总开关与结果封装，本模块按固定顺序完成**本地**预检：

1. 三参数类型校验（ID 一律按字符串处理，禁止数值换算）；
2. 用一次性凭据换回可信 Planner 会话（拒绝模型伪造的会话/平台/授权字段）；
3. 校验来源为 QQ 群聊且在源白名单，并按可信会话查询源消息归属与可读性；
4. 校验目标群白名单、不同于源群（**不再要求宿主目标发送会话**：目标平台与账号
   可用性由认证服务独立核对）；
5. 构建材料摘要：普通消息继续做完整内容预检（含提及文本化与不支持段拒绝），
   **合并转发卡片不走本地递归节点校验**（只判形状；内部资源是否可用由服务端从
   可信源消息取得并决定，design D3/D9）。

任一步失败都在产生任何发送副作用前整条拒绝，拒绝原因统一经 `Rejected.not_sent`
补「本次未发送任何消息」定尾；全部通过时返回 `PreparedForward`，由客户端投递编排
（`forward_delivery.py`）交给认证服务执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .send_material import SendMaterial, UnsupportedContent, build_send_material, is_forward_card
from .source_binding import (
    ForwardArguments,
    Rejected,
    SourceBindingTable,
    parse_forward_arguments,
)

QQ_PLATFORM = "qq"
"""宿主 QQ 平台名：来源会话必须属于该平台（spec 的 QQ 范围）。"""

ALL_PLATFORMS = "all_platforms"
"""宿主 `chat.get_all_streams` 的全平台取值（`data.py:_list_sessions`）。"""

CARD_MATERIAL_KIND = "forward"
"""合并转发卡片的材料种类（`send_material.SendKind` 的同名字面量）。"""

CARD_MATERIAL_SHAPE: tuple[str, ...] = ("forward",)
"""卡片材料摘要的结构指纹：只表示「一张卡片」，不含节点正文与内部结构。"""


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

    普通消息直接用 `send_material.SendMaterial`；合并转发卡片只记形状，避免在
    本地递归展开节点（design D3）。
    """

    kind: str
    shape: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedForward:
    """预检通过的可发送计划（本地不发送，交由认证服务执行）。

    `material` 只用于工具结果的 `send_kind` / `material_shape` 展示；源消息正文与
    资源标识不由客户端提交给服务端（服务端从可信源消息自行取得，design D2）。
    """

    arguments: ForwardArguments
    source: SourceSession
    material: SendMaterial | MaterialDigest


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
        return Rejected.not_sent("调用来源群不在源群白名单内")

    message = await _fetch_source_message(ctx, parsed.message_id, source.session_id)
    if isinstance(message, Rejected):
        return message

    if parsed.target_group_id == source.group_id:
        return Rejected.not_sent("目标群不能是调用来源群自身")
    if not target_is_allowed(config, parsed.target_group_id):
        return Rejected.not_sent("目标群不在目标白名单内")

    material = _material_digest(message.get("raw_message"))
    if isinstance(material, Rejected):
        return material

    return PreparedForward(arguments=parsed, source=source, material=material)


def _material_digest(raw_message: Any) -> SendMaterial | MaterialDigest | Rejected:
    """构建材料摘要：卡片只判顶层形状（免本地递归），普通消息仍做完整内容预检。

    卡片只检查**顶层**形态：唯一一张卡片（含宿主未展开的 `dict` 包装）可以免去本地
    递归节点校验，内部资源是否可用由服务端从可信源消息决定；但卡片与其它顶层内容
    混排无法保持卡片形态，在本地即整条拒绝（design D3 的既有规则，不是递归校验）。
    """

    if not isinstance(raw_message, list) or not raw_message:
        return Rejected.not_sent("源消息没有可发送的内容段")
    cards = [segment for segment in raw_message if is_forward_card(segment)]
    if cards:
        if len(raw_message) != 1:
            return Rejected.not_sent(
                "合并转发卡片与其它顶层内容混排，无法保持卡片形态，整条拒绝"
            )
        return MaterialDigest(kind=CARD_MATERIAL_KIND, shape=CARD_MATERIAL_SHAPE)
    material = build_send_material(raw_message)
    if isinstance(material, UnsupportedContent):
        return Rejected.not_sent(material.reason)
    return material


async def _resolve_source_session(ctx: Any, session_id: str) -> SourceSession | Rejected:
    """按可信会话 ID 在宿主会话表里解析平台、账号、作用域与群号。

    SDK 已把 `chat.get_all_streams` 归一为会话列表（`context.py:34-…`
    `_CAPABILITY_RESULT_KEYS`），宿主异常在 Runner 侧表现为异常。
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


def whitelist_entries(values: Any) -> tuple[str, ...]:
    """把配置白名单归一成去空白字符串序列（字符串化、丢弃空值、保持配置顺序）。

    工具侧预检、权限复验与自动入口共用本口径：用户可能手工编辑配置文件，判定不得
    因为一处实现只接受 `str` 而在另一处接受字符串化结果。
    """

    try:
        items = list(values)
    except TypeError:
        return ()
    return tuple(text for item in items if (text := str(item).strip()))


def source_group_is_allowed(config: Any, group_id: str) -> bool:
    """来源群是否仍在源群白名单内（与目标群判定共用同一份白名单口径）。"""

    return str(group_id).strip() in set(whitelist_entries(config.scope.source_groups))


def target_is_allowed(config: Any, target_group_id: str) -> bool:
    """目标群是否仍在目标白名单内。

    预检、源阶段授权前的复验与评论阶段授权前的复验共用同一口径：复验必须读提交
    时的最新配置，不得使用调用开始时的旧快照。
    """

    return str(target_group_id).strip() in set(whitelist_entries(config.scope.target_groups))
