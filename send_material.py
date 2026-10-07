"""宿主与 OneBot 源材料转换：共享提及呈现，只输出材料与结构指纹，不发送或落盘。

两个输入面分别映射：宿主 `build_send_material` 校验并重建宿主消息材料（普通图片
须有 `binary_data_base64`，错误定位到段或节点）；OneBot `build_source_material` /
`source_outbound_segments` 只使用可信 OneBot 源材料：普通消息校验图片引用并按
`verbatim_mentions` 分流：图片路径把提及文本化，文字路径原样保留 at。相邻文本
归并不跨越原有分段、不新增换行；对象不明或材料不可完整发送时整条拒绝。

顶层合并转发卡片由 `classify_top_level_shape` 判定唯一形状后交给
`build_card_nodes`：从 `get_forward_msg` 展开查询结果构造 `send_group_forward_msg`
节点数组——节点 `uin` 取平台的 sender `user_id`、显示名取 `card` 优先回退
`nickname` 并压平限长、内容段原样深拷贝；嵌套 forward 段在到达本函数前已被
`type_check` 的发送前类型检查拒绝（本函数不再接收嵌套内容，函数行为未变：
content 段仍原样深拷贝）。节点材料只来自展开查询，不接受调用者提交的资源 ID、
正文或媒体地址。

「本次未发送任何消息」定尾由工具入口的 `Rejected.not_sent` 统一补齐。
"""

from __future__ import annotations

import re
import ipaddress
import socket
from urllib.parse import urlsplit
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .shared.identifiers import verbatim_identifier
from .shared.mentions import flatten_mention_text, mention_display_text
from .shared.send_steps import PrepareRejected, SourceKind, SourceMaterial


def classify_top_level_shape(raw_message: list[Any]) -> Literal["forward", "mixed", "plain"]:
    """顶层形态判定的单一口径（预检、自动入口与客户端三处共用）。

    - `forward`：恰好一张合并转发卡片（含未展开 `dict` 包装）——卡片发送材料
      由 `build_card_nodes` 从展开查询构造，插件不提取资源 ID；
    - `mixed`：卡片与其它顶层内容混排——无法保持卡片形态，整条拒绝；
    - `plain`：无卡片——按普通消息段口径继续（`build_source_material` 或宿主
      `build_send_material`，两种输入面各自的材料构造）。

    只判顶层、不递归卡片内部；卡片唯一形状判据是 `is_forward_card`。
    """

    cards = [segment for segment in raw_message if is_forward_card(segment)]
    if not cards:
        return "plain"
    return "forward" if len(raw_message) == 1 else "mixed"


def build_source_material(message: object, *, verbatim_mentions: bool = False) -> SourceMaterial | PrepareRejected:
    """OneBot 源记录的纯转换入口（普通消息），与宿主序列化形状分开处理。

    `verbatim_mentions=False`（默认）服务普通图片直发路径：个人与全体成员提及
    文本化成不触发提醒的普通文本；`verbatim_mentions=True` 服务单节点卡片的文字
    路径：at 段按源消息原样保留，不文本化、不改写。两种口径共用同一套段校验
    （顺序保留、不支持段/缺媒体/回复段整条拒绝、提及对象可确认）。

    顶层合并转发卡片不走本函数（卡片发送路径由 `build_card_nodes` 从展开查询
    构造节点）；进入本函数的卡片段一律整条拒绝。
    """

    if not isinstance(message, list) or not message:
        return PrepareRejected("source_empty", "源消息没有可发送的内容段，发送前拒绝")
    shape = classify_top_level_shape(message)
    if shape == "mixed":
        return PrepareRejected("source_shape_unsupported", "顶层卡片与其它内容混排，整条拒绝")
    if shape == "forward":
        return PrepareRejected("source_shape_unsupported", "卡片发送路径不走段材料构造，整条拒绝")
    pieces: list[_TextPiece | _SegmentPiece] = []
    for segment in message:
        if not isinstance(segment, dict):
            return PrepareRejected("source_segment_unsupported", "普通源消息结构无效，整条拒绝")
        kind, data = segment.get("type"), segment.get("data")
        if kind == "text" and isinstance(data, dict) and isinstance(data.get("text"), str):
            text = data["text"]
        elif kind == "at" and isinstance(data, dict):
            raw = data.get("qq")
            identifier = flatten_mention_text(str(raw) if type(raw) is int else raw)
            if (isinstance(raw, bool) or not identifier or
                    (identifier != "all" and not (identifier.isascii() and identifier.isdigit() and any(c != "0" for c in identifier)))):
                return PrepareRejected("source_mention_unconfirmed", "提及对象不可确认，整条拒绝")
            if verbatim_mentions:
                pieces.append(_SegmentPiece(deepcopy(segment), "at"))
                continue
            name = next((value for field in ("card", "nickname", "name")
                         if (value := flatten_mention_text(data.get(field)))), "")
            text = mention_display_text(identifier, name)
        elif kind == "image" and isinstance(data, dict):
            url = _safe_media_url(data.get("url"))
            if not url:
                return PrepareRejected("source_media_unsafe", "图片没有可安全重放的 HTTPS 媒体引用，整条拒绝")
            pieces.append(_SegmentPiece({"type": "image", "data": {"url": url}}, "image"))
            continue
        else:
            return PrepareRejected("source_segment_unsupported", "普通源消息含不支持或不完整段，整条拒绝")
        pieces.append(_TextPiece(text, kind == "at", {"type": "text", "data": text}))
    built = _assemble_segments(pieces)
    segments = tuple({"type": "text", "data": {"text": s["data"]}} if s["type"] == "text" else s for s in built.segments)
    return SourceMaterial(kind=_dispatch_kind(segments), segments=segments)


def _dispatch_kind(segments: tuple[dict[str, Any], ...]) -> SourceKind:
    """普通消息的发送路径取值：含图片段 → image，否则 → text。"""

    return "image" if any(str(segment.get("type") or "") == "image" for segment in segments) else "text"

def _safe_media_url(raw: object) -> str:
    """媒体字面量边界：只放行可安全重放的 HTTPS 地址；本机不下载、不请求该地址。"""
    if not isinstance(raw, str) or not raw or "\\" in raw or any(not 0x21 <= ord(c) <= 0x7e for c in raw):
        return ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").rstrip(".").lower()
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.fragment or not host or parts.port == 0):
            return ""
    except ValueError:
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton 只解析数字字面量，不进行 DNS；传统混淆写法一律拒绝。
        try:
            socket.inet_aton(host)
        except (OSError, ValueError):
            pass
        else:
            return ""
        if ("." not in host or host.endswith((".localhost", ".local", ".internal"))
                or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))
                or not any(c.isalpha() for c in host.rsplit(".", 1)[-1])):
            return ""
    else:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or address.is_reserved:
            return ""
    return raw


def source_outbound_segments(material: SourceMaterial) -> tuple[dict[str, Any], ...] | PrepareRejected:
    """冻结材料的输出段：卡片发送路径不走段重建（发送动作按展开查询构造的节点提交）。

    普通路径的输出段已在 `build_source_material` 一次构造完成（图片路径 at 已
    文本化；文字路径的材料已是单节点卡片数组=平台载荷终点，content 含 at 原样）；
    本函数只做深拷贝隔离后续改动，不再二次校验或重建（材料是冻结产物，出口唯一）。
    """

    if material.kind == "forward":
        return PrepareRejected("source_shape_unsupported", "卡片发送路径不重建段材料，未进入平台调用")
    return tuple(deepcopy(segment) for segment in material.segments)


SENDER_NAME_MAX_LENGTH = 128
"""卡片节点署名显示名上限：压平控制字符后截断，不引入省略号。"""


def build_card_nodes(expanded: object) -> tuple[dict[str, Any], ...] | PrepareRejected:
    """从 `get_forward_msg` 展开查询结果构造 `send_group_forward_msg` 节点数组。

    输入是展开查询的 `data`（含 `messages` 键）或 `messages` 节点列表本身。每个
    节点只取平台返回的三个字段：`uin` 取 sender 对象 `user_id`（压平后逐字校验，
    不可确认整条拒绝），显示名取 `card` 优先、回退 `nickname`（压平并限长），
    内容段为 `message` 段数组原样深拷贝。

    字段名是 `uin` 而非 `sender`：平台 OneBot 层自定义节点分支只读
    `user_id ?? uin` 与 `nickname ?? name`（源码
    `packages/onebot/src/modules/message-actions.ts` `parseForwardNodes`），
    `sender` 只被元数据标量校验放行、解析时静默丢弃，uin 回退登录账号自身。
    2026-10-06 对运行实例的对照实测证实：`sender` 标量 + `name` 投递后节点发送者
    是 bot 自己，`uin` + `name` 与 `user_id` + `nickname` 均保留原发送者。
    文字路径 `snowluma_client._text_card_node` 同口径使用 `uin`/`name`。

    嵌套 forward 段随内容原样深拷贝（函数口径不因调用方而变）；但生产链路上嵌套
    内容在到达本函数前已被 `type_check` 的发送前类型检查拒绝——实测嵌套内层在
    目标群客户端只能显示为降级空壳，与所引用资源的新旧无关（ADR 0011），故
    `build_card_nodes` 不再接收嵌套内容，此处原样拷贝只是函数自身口径。不设置
    `summary`/`news`/`prompt`/`source`
    预览参数；节点材料只来自本次展开查询，不接受调用者提交的资源 ID、消息正文
    或媒体地址。
    """

    messages = expanded.get("messages") if isinstance(expanded, Mapping) else expanded
    if not isinstance(messages, list) or not messages:
        return PrepareRejected("forward_expand_unavailable", "展开查询没有可用的卡片节点，发送前拒绝")
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(messages, start=1):
        if not isinstance(item, Mapping):
            return PrepareRejected("forward_node_invalid", f"展开查询第 {index} 个节点结构无效，发送前拒绝")
        sender = card_node_sender(item.get("sender"))
        if not sender:
            return PrepareRejected("sender_unconfirmed", f"展开查询第 {index} 个节点发送者标识不可确认，整条拒绝")
        name = card_node_name(item.get("sender"))
        if not name:
            return PrepareRejected("sender_name_unconfirmed", f"展开查询第 {index} 个节点发送者显示名不可确认，整条拒绝")
        content = item.get("message")
        if (not isinstance(content, list) or not content
                or not all(isinstance(segment, dict) for segment in content)):
            return PrepareRejected("forward_node_invalid", f"展开查询第 {index} 个节点内容不是可用的段数组，整条拒绝")
        nodes.append({"type": "node", "data": {
            "uin": sender,
            "name": name,
            "content": [deepcopy(segment) for segment in content],
        }})
    return tuple(nodes)


def card_node_sender(sender: object) -> str:
    """卡片节点 `uin` 标量：只取 sender 对象的 `user_id`，压平后逐字校验。

    卡片展开重组与文字单节点卡片两条路径共用本口径：含空白/控制字符/过长一律
    视为不可确认（不净化成另一个字符串），由调用方拒绝。平台解析自定义节点时
    只读 `user_id`/`uin`（实测与源码一致）；`sender` 字段被平台静默丢弃、uin
    回退 bot 自身，因此节点署名字段用 `uin`。
    """

    raw = sender.get("user_id") if isinstance(sender, Mapping) else None
    if isinstance(raw, (str, int)) and not isinstance(raw, bool):
        return verbatim_identifier(flatten_mention_text(str(raw)))
    return ""


def card_node_name(sender: object) -> str:
    """卡片节点 `name` 显示名：`card` 优先、回退 `nickname`，压平并限长。

    卡片展开重组与文字单节点卡片两条路径共用本口径（不查询目标群成员补全）。
    空名分支按路径分流：文字路径留空、由平台决定展示；卡片展开路径
    （`build_card_nodes`）整条拒绝——卡片节点署名必须可确认，不用空名称蒙混过
    平台校验。
    """

    if isinstance(sender, Mapping):
        for field_name in ("card", "nickname"):
            name = flatten_mention_text(sender.get(field_name))
            if name:
                return name[:SENDER_NAME_MAX_LENGTH]
    return ""


MAX_FORWARD_DEPTH = 3
"""合并转发卡片最大层数（宿主消息递归预检上限）：超出即整条拒绝。"""

EMPTY_FORWARD_NODE_PLACEHOLDER = "[empty forward node]"
"""宿主对空转发节点的占位文本。

宿主在解析期就把内容为空的节点换成这段文本，插件拿不到原文。节点内的这段文本
因此必须视为不可恢复节点整条拒绝，不能当作普通正文转发；顶层消息里的同名字符串
是真实正文，不受此规则影响。
"""

_SAFE_SEGMENT_LABEL = re.compile(r"[a-z0-9_]{1,16}\Z")
"""可安全回显的段类型词法：短小、纯 ASCII 小写；其余一律显示为「未知」。"""

_AT_SEGMENT_TYPE = "at"
"""宿主提及段的类型名。"""

_REPLY_SEGMENT_TYPE = "reply"
"""宿主引用（回复）段的类型名：只作消息装饰，不能随消息整体转发。"""

# 提及格式与压平规则只定义在 shared/mentions.py；本模块分别映射宿主和 OneBot 字段。

SendKind = Literal["text", "image", "mixed", "forward"]
"""宿主材料种类：纯文本、纯图片、混合，或合并转发卡片。

与 `shared.send_steps.SourceKind`（可信 OneBot 源材料的分派三值）分工不同：本词汇
只描述宿主入站形状（图文混排是 `mixed`），分派词汇按平台动作收敛（含图片即
`image`）。两者服务不同输入面，互不替换；不要合并成一个 Literal。
"""


@dataclass(frozen=True, slots=True)
class UnsupportedContent:
    """内容不可完整发送：带可定位到段/节点/类型的中文原因。"""

    reason: str


@dataclass(frozen=True, slots=True)
class SendMaterial:
    """宿主消息的校验结果：普通段 `segments`、卡片节点 `nodes` 与无正文的 `shape`。

    实际投递只发送可信 OneBot 源材料，不把此处节点交给宿主发送或重建原卡片。
    """

    kind: SendKind
    segments: tuple[dict[str, Any], ...] = ()
    nodes: tuple[dict[str, Any], ...] = ()
    shape: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _BuiltSegments:
    """已校验的段序列与其结构指纹。"""

    segments: tuple[dict[str, Any], ...]
    shape: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _TextPiece:
    """待归并的文本片段：原文文本段或提及转换出的普通文本。

    `mention` 标记该片段是否由提及转换而来（决定相邻文本片段是否归并）；
    `segment` 是片段对应的独立文本段副本，归并时以其中「原文文本段」为基底，
    保留宿主可能附带的其它字段。
    """

    text: str
    mention: bool
    segment: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SegmentPiece:
    """不参与文本归并的已校验段（图片或合并转发卡片）与其结构指纹。"""

    segment: dict[str, Any]
    shape: Any


def is_forward_card(segment: Any) -> bool:
    """该段是否为合并转发卡片（含宿主未展开的 `dict` 包装形态）。

    展开形态是 `{"type": "forward", "data": [节点…]}`；宿主无法恢复节点材料时会
    收成 `DictComponent`，序列化后是 `{"type": "dict", "data": {"type": "forward", …}}`。
    自动入口（只需消息 ID 就能给判断机会）与工具预检共用本形状判据；实际卡片只
    检查顶层，不递归处理节点。
    """

    if not isinstance(segment, dict):
        return False
    if str(segment.get("type") or "").strip() == "forward":
        return True
    return _is_unexpanded_forward(segment)


def forward_card_id(segment: Any) -> str:
    """顶层卡片段的平台卡片标识：`get_forward_msg` 展开查询只按该标识进行。

    与 `is_forward_card` 同一形状口径：OneBot `{"type": "forward", "data": {"id": …}}`
    与未展开 `dict` 包装都取 `data.id`。标识逐字校验（含空白/控制字符/过长即不可
    确认），取不到由调用方拒绝：不用正文、资源引用或源消息 ID 猜造。
    """

    if not is_forward_card(segment):
        return ""
    card = segment
    if str(card.get("type") or "").strip() == "dict":
        card = card.get("data")
    data = card.get("data") if isinstance(card, Mapping) else None
    raw = data.get("id") if isinstance(data, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return ""
    return verbatim_identifier(str(raw))


def build_send_material(raw_message: Any) -> SendMaterial | UnsupportedContent:
    """校验源消息的原始段序列并构建发送材料。"""

    if not isinstance(raw_message, list) or not raw_message:
        return UnsupportedContent("源消息没有可发送的内容段")

    built = _build_segments(raw_message, location="源消息", depth=0)
    if isinstance(built, UnsupportedContent):
        return built
    if not built.segments:
        return UnsupportedContent("源消息没有可发送的内容段")

    forward_segments = [
        segment for segment in built.segments if str(segment.get("type") or "") == "forward"
    ]
    if forward_segments:
        if len(built.segments) != 1:
            return UnsupportedContent("合并转发与其它消息段混排，无法保持聊天记录卡片形态")
        card = forward_segments[0]
        raw_nodes = card.get("data")
        nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
        return SendMaterial(kind="forward", nodes=tuple(nodes), shape=built.shape)

    return SendMaterial(
        kind=_segments_kind(built.segments),
        segments=built.segments,
        shape=built.shape,
    )


def _build_segments(
    raw_segments: list[Any],
    *,
    location: str,
    depth: int,
) -> _BuiltSegments | UnsupportedContent:
    """按顺序校验段，返回材料的独立副本与结构指纹。

    `depth` 是当前段序列所在的卡片层数（顶层消息为 0）：节点内容里的转发段按
    层数继续递归，超过 `MAX_FORWARD_DEPTH` 即整条拒绝；`depth > 0` 同时表示
    「这段文本位于转发节点内」，宿主空节点占位文本只可能出现在这里。

    逐段校验（含占位文本检查）全部在归并之前完成：先按原文逐段判定，再把含提及的
    相邻文本片段归并成普通文本段（`_assemble_segments`），任何异常都不会因归并
    被洗掉。
    """

    pieces: list[_TextPiece | _SegmentPiece] = []
    for index, item in enumerate(raw_segments, start=1):
        segment_location = f"{location}第 {index} 段"
        if not isinstance(item, dict):
            return UnsupportedContent(f"{segment_location}结构无效，无法完整转发")

        segment_type = str(item.get("type") or "").strip()
        if segment_type == "text":
            text = item.get("data")
            if not isinstance(text, str):
                return UnsupportedContent(f"{segment_location}文本内容无效，无法完整转发")
            if depth > 0 and text == EMPTY_FORWARD_NODE_PLACEHOLDER:
                return UnsupportedContent(
                    f"{segment_location}是宿主对空节点的占位文本"
                    f"（{EMPTY_FORWARD_NODE_PLACEHOLDER}），原文无法恢复"
                )
            pieces.append(_TextPiece(text=text, mention=False, segment=deepcopy(item)))
            continue
        if segment_type == _AT_SEGMENT_TYPE:
            mention_text = _mention_display_text(item, segment_location)
            if isinstance(mention_text, UnsupportedContent):
                return mention_text
            pieces.append(
                _TextPiece(
                    text=mention_text,
                    mention=True,
                    segment={"type": "text", "data": mention_text},
                )
            )
            continue
        if segment_type == "image":
            if not _has_image_material(item):
                return UnsupportedContent(
                    f"{segment_location}图片缺少可恢复的媒体数据（描述文本不能代替原图）"
                )
            pieces.append(_SegmentPiece(deepcopy(item), "image"))
            continue
        if segment_type == "forward":
            if depth + 1 > MAX_FORWARD_DEPTH:
                return UnsupportedContent(
                    f"{segment_location}的合并转发嵌套超过 {MAX_FORWARD_DEPTH} 层，无法完整发送"
                )
            card = _build_card(item, location=segment_location, depth=depth + 1)
            if isinstance(card, UnsupportedContent):
                return card
            pieces.append(card)
            continue
        if _is_unexpanded_forward(item):
            return UnsupportedContent(f"{segment_location}是未展开的合并转发（宿主未能恢复节点）")

        if segment_type == _REPLY_SEGMENT_TYPE:
            # 引用段是消息装饰，不是可随消息整体转发的内容：群里「回复聊天记录卡片」
            # 的那条消息在气泡里显示被引用卡片的预览，宿主侧却只有 reply 段。这里给出
            # 可定位原因与下一步（改用被引用卡片自身的消息 ID），不读取或回显被引用内容。
            return UnsupportedContent(
                f"{segment_location}是引用（{_REPLY_SEGMENT_TYPE}）消息段，不支持随消息整体转发"
                + ("；要转发被引用的合并转发卡片，请使用该卡片自身的消息 ID" if depth == 0 else "")
                + "，无法完整转发"
            )

        return UnsupportedContent(f"{segment_location}类型 {_segment_label(item)} 无法完整发送")
    return _assemble_segments(pieces)


def _assemble_segments(pieces: list[_TextPiece | _SegmentPiece]) -> _BuiltSegments:
    """把已校验片段组装成材料段序列：含提及的相邻文本片段归并为一个文本段。

    归并范围只覆盖「紧邻提及的文本」：提及左侧只并入紧邻的那一个原文文本段、右侧
    只并入紧邻的那一个，两个原文文本段之间的既有分段（发送通路按换行拼接的排版）
    不因转换被跨过，也不顺带改写其它消息的排版。归并直接拼接片段内容、不插入分隔
    符，因此提及转换不会新增换行，原文里的换行原样保留；片段顺序不变，结构指纹
    描述归并后的实际材料形态。
    """

    segments: list[dict[str, Any]] = []
    shapes: list[Any] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        if isinstance(piece, _SegmentPiece):
            segments.append(piece.segment)
            shapes.append(piece.shape)
            index += 1
            continue

        # 归并范围只延伸到紧邻提及的文本段：两个原文文本段相邻即是源消息本来的
        # 分段边界，就此断开；边界另一侧的文本段保持独立分段。
        run = [piece]
        cursor = index + 1
        while cursor < len(pieces):
            following = pieces[cursor]
            if not isinstance(following, _TextPiece):
                break
            if not following.mention and not run[-1].mention:
                break
            run.append(following)
            cursor += 1
        index = cursor

        # run 长度 ≥2 时必然含提及（归并循环只在相邻片段之一为提及时才继续延伸）；
        # 长度 1 时没有可归并的邻居，直接追加该片段即可。
        if len(run) == 1:
            segments.append(piece.segment)
            shapes.append("text")
            continue

        # 以原文文本段为基底（保留宿主可能附带的其它字段），内容直接拼接。
        base = next((item.segment for item in run if not item.mention), run[0].segment)
        merged = dict(base)
        merged["data"] = "".join(item.text for item in run)
        segments.append(merged)
        shapes.append("text")
    return _BuiltSegments(tuple(segments), tuple(shapes))


def _mention_display_text(item: dict[str, Any], location: str) -> str | UnsupportedContent:
    """映射宿主提及字段并复用共享呈现；对象缺失、类型错误或压平后为空即整条拒绝。

    `all` 表示全体成员；个人名称取源群名片、其次昵称，不查询目标群补全。
    名称和标识的控制字符压平、三种文本格式统一由 `shared/mentions.py` 定义。
    """

    data = item.get("data")
    if not isinstance(data, dict):
        return UnsupportedContent(f"{location}的提及结构无效，无法确认提及对象")
    identifier = flatten_mention_text(data.get("target_user_id"))
    if not identifier:
        return UnsupportedContent(f"{location}的提及缺少可确认的对象标识，无法完整转发")
    name = flatten_mention_text(data.get("target_user_cardname")) or flatten_mention_text(
        data.get("target_user_nickname")
    )
    return mention_display_text(identifier, name)


def _build_card(forward_segment: dict[str, Any], *, location: str, depth: int) -> _SegmentPiece | UnsupportedContent:
    """递归校验合并转发的全部节点，重建完整聊天记录卡片段。"""

    raw_nodes = forward_segment.get("data")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return UnsupportedContent(f"{location}没有可恢复的节点，无法准备完整聊天记录卡片")

    nodes: list[dict[str, Any]] = []
    contents: list[Any] = []
    for index, node in enumerate(raw_nodes, start=1):
        node_location = f"{location} 第 {index} 个节点"
        if not isinstance(node, dict):
            return UnsupportedContent(f"{node_location}结构无效，无法完整转发")
        raw_content = node.get("content")
        if not isinstance(raw_content, list) or not raw_content:
            return UnsupportedContent(
                f"{node_location}内容为空（宿主会以占位文本降级），原文无法恢复"
            )

        built_content = _build_segments(raw_content, location=node_location, depth=depth)
        if isinstance(built_content, UnsupportedContent):
            return built_content

        rebuilt_node = deepcopy(node)
        rebuilt_node["content"] = list(built_content.segments)
        nodes.append(rebuilt_node)
        contents.append(list(built_content.shape))

    rebuilt_segment = deepcopy(forward_segment)
    rebuilt_segment["data"] = nodes
    return _SegmentPiece(segment=rebuilt_segment, shape={"forward": contents})


def _is_unexpanded_forward(item: dict[str, Any]) -> bool:
    """`dict` 包装的未展开转发：宿主没有恢复出节点材料。"""

    if str(item.get("type") or "").strip() != "dict":
        return False
    data = item.get("data")
    return isinstance(data, dict) and str(data.get("type") or "").strip() == "forward"


def _has_image_material(item: dict[str, Any]) -> bool:
    """图片必须具备可用的二进制材料（`binary_data_base64`）。"""

    binary_data = item.get("binary_data_base64")
    return isinstance(binary_data, str) and bool(binary_data)


def _segment_label(item: dict[str, Any]) -> str:
    """给出可安全回显的段类型标签；无法安全回显时返回「未知」。

    段类型来自消息材料（不可信）：只回显符合宿主段类型词法的短标签，避免把正文
    或节点里的任意文本带进工具结果（也就是模型上下文）。`dict` 包装时优先回显内层
    类型，便于定位视频等未展开组件。
    """

    raw_type = str(item.get("type") or "").strip()
    data = item.get("data")
    if raw_type == "dict" and isinstance(data, dict):
        inner_type = str(data.get("type") or "").strip()
        if _SAFE_SEGMENT_LABEL.fullmatch(inner_type):
            return inner_type
    if _SAFE_SEGMENT_LABEL.fullmatch(raw_type):
        return raw_type
    return "未知"


def _segments_kind(segments: tuple[dict[str, Any], ...]) -> SendKind:
    """按段类型组合给出材料种类：纯文本、纯图片或混合。"""

    kinds = {str(segment.get("type") or "") for segment in segments}
    if kinds == {"text"}:
        return "text"
    if kinds == {"image"}:
        return "image"
    return "mixed"
