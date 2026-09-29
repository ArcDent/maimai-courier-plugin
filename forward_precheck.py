"""转发请求的权限与内容预检（不发送任何消息）。

工具入口只负责总开关与结果封装，本模块按固定顺序完成预检：

1. 三参数类型校验（ID 一律按字符串处理，禁止数值换算）；
2. 用一次性凭据换回可信 Planner 会话（拒绝模型伪造的会话/平台/授权字段）；
3. 校验来源为 QQ 群聊且在源白名单，并按可信会话查询源消息归属；
4. 校验目标群白名单、非源群自身，并核对目标路由的账号与平台；
5. 校验完整发送材料（含合并转发节点递归预检）。

任一步失败都在产生发送副作用前整条拒绝，拒绝原因统一经 `Rejected.not_sent`
补「本次未发送任何消息」定尾；全部通过时返回 `PreparedForward`，由后续实现组
接通实际发送。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .send_material import SendMaterial, UnsupportedContent, build_send_material
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


@dataclass(frozen=True, slots=True)
class SourceSession:
    """可信来源会话：平台、账号、作用域与群号全部来自宿主会话表。"""

    session_id: str
    platform: str
    group_id: str
    account_id: str
    scope: str


@dataclass(frozen=True, slots=True)
class TargetRoute:
    """可信目标路由：由来源平台/账号/作用域与白名单目标群构造。"""

    session_id: str
    group_id: str
    platform: str
    account_id: str
    scope: str


@dataclass(frozen=True, slots=True)
class PreparedForward:
    """预检通过的可发送计划。

    实际发送（第 5 组）直接复用这里的材料与目标路由：`material.segments` 是普通
    消息的段序列，`material`（合并转发）含节点卡片，`target.session_id` 是核对过
    账号与平台的目标会话。
    """

    arguments: ForwardArguments
    source: SourceSession
    target: TargetRoute
    material: SendMaterial


@dataclass(frozen=True, slots=True)
class _StreamFields:
    """宿主会话字典的归一字段（同一口径供来源与目标路由共用）。"""

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
    """按顺序完成权限与内容预检；全部通过时返回 `PreparedForward`。

    任一步失败都在产生发送副作用前整条拒绝；本函数不发送任何消息。
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

    route = await _open_target_route(ctx, source, parsed.target_group_id)
    if isinstance(route, Rejected):
        return route

    material = build_send_material(message.get("raw_message"))
    if isinstance(material, UnsupportedContent):
        return Rejected.not_sent(material.reason)

    return PreparedForward(arguments=parsed, source=source, target=route, material=material)


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

    工具侧预检、评论/源发送前的权限复验与自动入口共用本口径：用户可能手工编辑
    配置文件，判定不得因为一处实现只接受 `str` 而在另一处接受字符串化结果。
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

    预检、源发送前的权限复验与「评论发送前的权限复验」共用同一口径：复验必须读
    发送时的最新配置，不得使用调用开始时的旧快照。
    """

    return str(target_group_id).strip() in set(whitelist_entries(config.scope.target_groups))


async def _open_target_route(
    ctx: Any,
    source: SourceSession,
    target_group_id: str,
) -> TargetRoute | Rejected:
    """打开（或取回）目标群会话，并核对平台、账号与群号一致。

    只按「可信来源账号 + 白名单目标群」构造路由，不按群号遍历历史会话：同群但
    属于其它账号的既有会话不会被当作当前路由；冷会话由宿主在这里创建，后续发送
    直接复用该路由。
    """

    try:
        response = await ctx.chat.open_session(
            platform=source.platform,
            chat_type="group",
            group_id=target_group_id,
            account_id=source.account_id,
            scope=source.scope,
        )
    except Exception:
        return Rejected.not_sent("无法验证目标群路由")

    if not isinstance(response, dict) or not response.get("success"):
        return Rejected.not_sent("无法验证目标群路由")
    stream = response.get("stream")
    if not isinstance(stream, dict):
        stream = response
    if not stream.get("is_group_session"):
        return Rejected.not_sent("目标路由不是群聊会话")

    fields = _stream_fields(stream)
    if not fields.session_id:
        return Rejected.not_sent("目标群路由缺少可确认的会话 ID")
    if fields.platform != source.platform:
        return Rejected.not_sent("目标路由平台与来源平台不一致")
    if fields.group_id != target_group_id:
        return Rejected.not_sent("目标路由群号与指定目标群不一致")
    if fields.account_id != source.account_id:
        return Rejected.not_sent("目标路由账号与来源账号不一致")

    return TargetRoute(
        session_id=fields.session_id,
        group_id=target_group_id,
        platform=source.platform,
        account_id=source.account_id,
        scope=fields.scope,
    )
