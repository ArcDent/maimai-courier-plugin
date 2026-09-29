"""转发内容完整性预检：只接受可完整恢复并原样发送的消息材料。

预检的目标是「不丢段、不降级、不以描述冒充媒体」：普通消息逐段校验并保留宿主
接收侧序列化形态；合并转发按节点递归校验，全部可恢复时才准备聊天记录卡片。
任一段不可用时整条拒绝，绝不发送缺失媒体、也绝不只发送一部分。

- 图片必须带 `binary_data_base64`：宿主序列化时只有内存二进制存在、或 hash 命中
  图片库且文件存在才会给出该字段（`message_utils.py:168-198`），缺失时发送侧
  `to_seg()` 会抛错，描述文本不能代替原图；
- 未展开的合并转发会被宿主收成 `DictComponent`（`message_utils.py:327-328`）；
  空内容节点会被宿主在解析期替换成占位文本 `[empty forward node]`
  （`:312-317`，字面量在 `:317`），DB 回读也无法还原原文
  （`message_component_data_model.py:359` 要求节点内容非空）：两者都是降级路径，
  预检一经发现即整条拒绝（顶层消息里的同名字符串是真实正文，不算占位）；
- 卡片层数上限对齐网关 `MAX_FORWARD_DEPTH`（SnowLuma
  `packages/onebot/src/modules/message-actions.ts:1484,1570-1577`）。

本模块只产出材料与结构指纹，不发送、不落盘；拒绝原因只描述内容本身，
「本次未发送任何消息」定尾由工具入口的 `Rejected.not_sent` 统一补齐。
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_FORWARD_DEPTH = 3
"""合并转发卡片最大层数：与网关递归深度上限一致，超出即整条拒绝。"""

EMPTY_FORWARD_NODE_PLACEHOLDER = "[empty forward node]"
"""宿主对空转发节点的占位文本（`message_utils.py:312-317`，字面量在 `:317`）。

宿主在解析期就把内容为空的节点换成这段文本，插件拿不到原文。节点内的这段文本
因此必须视为不可恢复节点整条拒绝，不能当作普通正文转发；顶层消息里的同名字符串
是真实正文，不受此规则影响。
"""

_SAFE_SEGMENT_LABEL = re.compile(r"[a-z0-9_]{1,16}\Z")
"""可安全回显的段类型词法：短小、纯 ASCII 小写；其余一律显示为「未知」。"""

SendKind = Literal["text", "image", "mixed", "forward"]
"""发送材料种类：纯文本、纯图片、混合，或合并转发卡片。"""


@dataclass(frozen=True, slots=True)
class UnsupportedContent:
    """内容不可完整发送：带可定位到段/节点/类型的中文原因。"""

    reason: str


@dataclass(frozen=True, slots=True)
class SendMaterial:
    """预检通过的发送材料。

    - `segments`：普通消息的段序列（宿主接收侧序列化形态，可直接交给发送链）；
    - `nodes`：合并转发的节点列表（同样可直接交给 `send.forward`）；
    - `shape`：与材料同构的结构指纹（只有段类型与嵌套、不含正文），用于在不发送
      的前提下验证材料完整性。
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
class _BuiltCard:
    """已校验的合并转发段（重建后的段）与其节点内容指纹。"""

    segment: dict[str, Any]
    contents: tuple[Any, ...]


def is_forward_card(segment: Any) -> bool:
    """该段是否为合并转发卡片（含宿主未展开的 `dict` 包装形态）。

    展开形态是 `{"type": "forward", "data": [节点…]}`；宿主无法恢复节点材料时会
    收成 `DictComponent`（`message_utils.py:327-338`），序列化后是
    `{"type": "dict", "data": {"type": "forward", …}}`。自动入口（只需消息 ID 就能
    给判断机会）与内容预检（要求节点可完整恢复）共用本形状判据，避免宿主序列化
    形态变化时一处放行、一处拒绝。
    """

    if not isinstance(segment, dict):
        return False
    if str(segment.get("type") or "").strip() == "forward":
        return True
    return _is_unexpanded_forward(segment)


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
    """

    segments: list[dict[str, Any]] = []
    shapes: list[Any] = []
    for index, item in enumerate(raw_segments, start=1):
        segment_location = f"{location}第 {index} 段"
        if not isinstance(item, dict):
            return UnsupportedContent(f"{segment_location}结构无效，无法完整转发")

        segment_type = str(item.get("type") or "").strip()
        if segment_type == "text":
            if not isinstance(item.get("data"), str):
                return UnsupportedContent(f"{segment_location}文本内容无效，无法完整转发")
            if depth > 0 and item["data"] == EMPTY_FORWARD_NODE_PLACEHOLDER:
                return UnsupportedContent(
                    f"{segment_location}是宿主对空节点的占位文本"
                    f"（{EMPTY_FORWARD_NODE_PLACEHOLDER}），原文无法恢复"
                )
            segments.append(deepcopy(item))
            shapes.append("text")
            continue
        if segment_type == "image":
            if not _has_image_material(item):
                return UnsupportedContent(
                    f"{segment_location}图片缺少可恢复的媒体数据（描述文本不能代替原图）"
                )
            segments.append(deepcopy(item))
            shapes.append("image")
            continue
        if segment_type == "forward":
            if depth + 1 > MAX_FORWARD_DEPTH:
                return UnsupportedContent(
                    f"{segment_location}的合并转发嵌套超过 {MAX_FORWARD_DEPTH} 层，无法完整发送"
                )
            card = _build_card(item, location=segment_location, depth=depth + 1)
            if isinstance(card, UnsupportedContent):
                return card
            segments.append(card.segment)
            shapes.append({"forward": list(card.contents)})
            continue
        if _is_unexpanded_forward(item):
            return UnsupportedContent(f"{segment_location}是未展开的合并转发（宿主未能恢复节点）")

        return UnsupportedContent(f"{segment_location}类型 {_segment_label(item)} 无法完整发送")
    return _BuiltSegments(tuple(segments), tuple(shapes))


def _build_card(forward_segment: dict[str, Any], *, location: str, depth: int) -> _BuiltCard | UnsupportedContent:
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
    return _BuiltCard(segment=rebuilt_segment, contents=tuple(contents))


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
