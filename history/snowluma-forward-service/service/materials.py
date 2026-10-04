"""可信源材料构建：从网关源消息派生可发送材料（design D3/D8，4.2 起）。

输入是绑定账号的网关 `get_msg` 返回的**存储副本**（OneBot 形态段序列），不是客户端
提交的任何内容。输出是共享层 `SourceMaterial`：

- `forward`：只取唯一顶层 `forward.data.id`（1.2 核实：收/发只认 `data.id`），
  不递归、不展开、不清洗内部节点；顶层混排整条拒绝；
- `text` / `image` / `mixed`：逐段校验并保留顺序，普通提及转成不提醒的普通文本
  （4.4 起），图片只接受可安全重放的 https 媒体引用（4.5 起）；任一不支持段、
  缺失媒体或不可确认提及即整条拒绝，绝不静默丢弃后继续发送。

本模块只做纯转换：不发送、不落盘、不读文件、不连网、不查目标群成员。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Final, Mapping
from urllib.parse import urlsplit

from shared.identifiers import MAX_IDENTIFIER_LENGTH, verbatim_identifier
from shared.mentions import (
    ALL_MENTION_IDENTIFIER,
    ALL_MENTION_TEXT,
    flatten_mention_text,
    mention_display_text,
)
from shared.protocol import ERROR_INVALID_ID
from shared.send_steps import SourceKind, SourceMaterial

ERROR_SOURCE_NOT_FOUND: Final[str] = "source_not_found"
"""源消息在网关缓存中不存在（缓存缺失：不重建、不猜造）。"""

ERROR_SOURCE_QUERY_UNAVAILABLE: Final[str] = "source_query_unavailable"
"""源消息查询不可用（未取得可用数据）。"""

ERROR_SOURCE_NOT_GROUP: Final[str] = "source_not_group"
"""源消息不是群聊消息（私聊源不转发）。"""

ERROR_SOURCE_GROUP_MISMATCH: Final[str] = "source_group_mismatch"
"""源消息实际属于另一群（群聊归属不符）。"""

ERROR_SOURCE_EMPTY: Final[str] = "source_empty"
"""源消息没有可发送的内容段。"""

ERROR_SOURCE_RESOURCE_MISSING: Final[str] = "source_resource_missing"
"""源卡片缺少可用的资源引用（空/缺失 `forward.data.id`）。"""

ERROR_SOURCE_SHAPE_UNSUPPORTED: Final[str] = "source_shape_unsupported"
"""顶层形态不支持（卡片与其它内容混排）。"""

ERROR_SOURCE_SEGMENT_UNSUPPORTED: Final[str] = "source_segment_unsupported"
"""普通消息混入不支持段（回复、文件、语音等）：整条拒绝，不静默丢弃。"""

ERROR_SOURCE_MENTION_UNCONFIRMED: Final[str] = "source_mention_unconfirmed"
"""提及对象不可确认（缺标识、空标识或结构非法）：整条拒绝，不猜成全体成员。"""

ERROR_SOURCE_MEDIA_UNSAFE: Final[str] = "source_media_unsafe"
"""图片媒体引用不可安全重放（`file://`、`base64://`、本机路径、明文 http、回环/内网地址）。"""

ERROR_TARGET_UNAVAILABLE: Final[str] = "target_unavailable"
"""目标群不可确认（源群自身或不在绑定账号可见的群名册中）。"""

# 提及呈现常量与格式（`ALL_MENTION_IDENTIFIER` / `ALL_MENTION_TEXT` /
# `mention_display_text` / `flatten_mention_text`）的唯一实现在共享层
# `shared/mentions.py`（design D9）：这里从共享层导入并保持本模块既有的公开名。
# 本模块只保留 OneBot 侧的字段映射与对象可确认性判定。

MENTION_NAME_FIELDS: Final[tuple[str, ...]] = ("card", "nickname", "name")
"""可选的提及显示名字段（1.3 核实发侧可选字段；收侧通常只有 `qq`）。

只作为**源材料**里的可选显示名使用：缺失时回退到对象标识，绝不查询目标群成员。
"""


@dataclass(frozen=True, slots=True)
class _TextPiece:
    """待归并的文本片段：原文文本段或提及转换出的普通文本。"""

    text: str
    mention: bool


@dataclass(frozen=True, slots=True)
class _MediaPiece:
    """不参与文本归并的已校验媒体段（图片）。"""

    segment: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PrepareRejected:
    """源准备被拒绝：只含机器可读类别与可定位中文原因，不含源正文。"""

    error: str
    reason: str


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """源准备通过的材料与定位（不发送、不落盘）。

    `material` 只来自绑定账号的网关查询；定位字段用于账本与结果面，不是防重键。
    """

    material: SourceMaterial
    source_group_id: str
    source_message_id: str
    target_group_id: str


def is_forward_segment(segment: object) -> bool:
    """该段是否为合并转发卡片段（OneBot 形态 `{"type": "forward", ...}`）。

    1.2 核实：OneBot 收/发 `forward` 段只认 `data.id`；`dict` 包装等未展开形态不是
    网关的 OneBot 段形态，按不支持段拒绝（不做宿主侧形态兼容）。
    """

    return isinstance(segment, Mapping) and str(segment.get("type") or "").strip() == "forward"


def build_source_material(message: object) -> SourceMaterial | PrepareRejected:
    """把网关源消息段序列校验成可发送材料；任一不可完整发送即整条拒绝。"""

    if not isinstance(message, list) or not message:
        return PrepareRejected(ERROR_SOURCE_EMPTY, "源消息没有可发送的内容段，发送前拒绝")
    cards = [segment for segment in message if is_forward_segment(segment)]
    if cards:
        if len(message) != 1:
            return PrepareRejected(
                ERROR_SOURCE_SHAPE_UNSUPPORTED,
                "源消息顶层含合并转发卡片与其它内容混排，无法保持卡片形态，整条拒绝",
            )
        return _card_material(cards[0])

    pieces: list[_TextPiece | _MediaPiece] = []
    for index, segment in enumerate(message, start=1):
        if not isinstance(segment, Mapping):
            return PrepareRejected(
                ERROR_SOURCE_SEGMENT_UNSUPPORTED, f"源消息第 {index} 段结构无效，整条拒绝"
            )
        segment_type = str(segment.get("type") or "").strip()
        if segment_type == "text":
            text = _text_payload(segment)
            if text is None:
                return PrepareRejected(
                    ERROR_SOURCE_SEGMENT_UNSUPPORTED,
                    f"源消息第 {index} 段文本结构无效，整条拒绝",
                )
            pieces.append(_TextPiece(text=text, mention=False))
            continue
        if segment_type == "at":
            mention_text = _mention_text(segment)
            if mention_text is None:
                return PrepareRejected(
                    ERROR_SOURCE_MENTION_UNCONFIRMED,
                    f"源消息第 {index} 段提及缺少可确认的对象标识，整条拒绝（不猜成全体成员）",
                )
            pieces.append(_TextPiece(text=mention_text, mention=True))
            continue
        if segment_type == "image":
            image = _image_segment(segment)
            if image is None:
                return PrepareRejected(
                    ERROR_SOURCE_MEDIA_UNSAFE,
                    f"源消息第 {index} 段图片缺少可安全重放的媒体引用"
                    "（只接受 https 地址，不读取本机文件或内网资源），整条拒绝",
                )
            pieces.append(_MediaPiece(image))
            continue
        return PrepareRejected(
            ERROR_SOURCE_SEGMENT_UNSUPPORTED,
            f"源消息第 {index} 段类型 {_segment_label(segment_type)} 无法完整发送，整条拒绝",
        )
    segments = _assemble_pieces(pieces)
    if not segments:
        return PrepareRejected(ERROR_SOURCE_EMPTY, "源消息没有可发送的内容段，发送前拒绝")
    return SourceMaterial(kind=_segments_kind(segments), segments=tuple(segments))


def _assemble_pieces(pieces: list[_TextPiece | _MediaPiece]) -> list[dict[str, Any]]:
    """把已校验片段组装成发送段：含提及的相邻文本片段归并为一个文本段。

    归并范围与 `send_material._assemble_segments` 同一语义：提及左侧只并入紧邻的
    那一个原文文本段、右侧只并入紧邻的那一个，两个原文文本段之间的既有分段不因
    转换被跨过；拼接连结不插入分隔符，因此转换不新增换行，原换行原样保留。
    媒体段（图片）不参与归并，顺序不变。
    """

    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        if isinstance(piece, _MediaPiece):
            segments.append(piece.segment)
            index += 1
            continue
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
        segments.append({"type": "text", "data": {"text": "".join(item.text for item in run)}})
    return segments


def _mention_text(segment: Mapping[str, Any]) -> str | None:
    """把已核实形态的提及段转成不触发提醒的普通文本；对象不可确认返回 `None`。

    对象标识必须可确认：恰为网关字面量 `all`，或正整数 uin（1.3 核实收侧 `qq`
    只输出 `all` 或 uin 十进制串）。`0`、负值、非十进制串等一律按「对象不可确认」
    整条拒绝，不猜成全体成员、也不净化成对不上真实对象的另一个标识。显示名只取自
    源材料里的可选字段（群名片优先、其次昵称、再次 name），缺失时只呈现对象标识，
    绝不查询目标群成员补全，也不虚构。显示名里的协议语法、命令或提示文本只作为
    普通文字发送，不改变路由与权限。呈现格式与压平口径见 `shared/mentions.py`
    （插件侧与服务端唯一实现）。
    """

    data = segment.get("data")
    if not isinstance(data, Mapping):
        return None
    raw_qq = data.get("qq")
    if isinstance(raw_qq, bool):
        return None
    if isinstance(raw_qq, int):
        identifier = str(raw_qq)
    elif isinstance(raw_qq, str):
        identifier = flatten_mention_text(raw_qq)
    else:
        return None
    if not identifier:
        return None
    if identifier == ALL_MENTION_IDENTIFIER:
        return ALL_MENTION_TEXT
    if not _positive_uin(identifier):
        return None
    name = ""
    for field in MENTION_NAME_FIELDS:
        candidate = flatten_mention_text(data.get(field))
        if candidate:
            name = candidate
            break
    return mention_display_text(identifier, name)


def _positive_uin(identifier: str) -> bool:
    """对象标识是否为可确认的 uin：限长、纯 ASCII 十进制、值大于 0（1.3 核实）。"""

    return (
        len(identifier) <= MAX_IDENTIFIER_LENGTH
        and identifier.isascii()
        and identifier.isdigit()
        and int(identifier) > 0
    )


def _image_segment(segment: Mapping[str, Any]) -> dict[str, Any] | None:
    """图片段 → 只带 https `url` 的安全发送段；媒体引用不可安全重放返回 `None`。

    1.3 核实：图片可重放表示只有 `{"type": "image", "data": {"url": <https…>}}`；
    `file` 会被当成本机路径、`file://`/`base64://`/本机路径都必须拒绝。
    """

    data = segment.get("data")
    url = _safe_media_url(data.get("url") if isinstance(data, Mapping) else None)
    if not url:
        return None
    return {"type": "image", "data": {"url": url}}


def _safe_media_url(raw: object) -> str:
    """可安全重放的媒体地址：可打印 ASCII 的 `https` 地址且主机不是本机/内网。

    只做字面量判定、不做 DNS 解析（不联网）：主机先按 IP 字面量归一（含
    `2130706433`、`0x7f000001`、`127.1`、八进制等传统写法，交给 `inet_aton`
    解析后按同一内网判据拒绝），域名形状的主机按外部地址处理。整个地址必须由
    可打印 ASCII 组成：零宽空格、软连字符等不可见字符既可能是绕过主机判定的
    夹带，也无法被网关当作合法地址，一律整条拒绝。反斜杠同样拒绝：WHATWG 解析器
    （网关侧）把它当路径分隔符，主机归属会与本服务的判定不一致。
    """

    if not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    if not candidate:
        return ""
    if any(not (0x21 <= ord(character) <= 0x7E) for character in candidate):
        return ""
    if "\\" in candidate:
        return ""
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme != "https":
        return ""
    if not host or _is_local_or_private_host(host):
        return ""
    return candidate


def _is_local_or_private_host(host: str) -> bool:
    """主机名是否不可安全交给网关（本机/内网字面量、或无法消歧的数字写法）。

    服务端自己不读取媒体地址（交给网关重放），但仍拒绝把本机/内网字面量交给网关，
    避免把任意本机文件或内网资源当媒体来源。直接可解析的 IP 字面量按内网判据处理；
    传统数字写法（`2130706433`、`0x7f000001`、`127.1`、八进制段）在不同解析器下
    可能落到不同地址（包括回环/内网），一律按不安全拒绝；不含 ASCII 字母或含非
    域名形状字符（如 `:` / `%`）又解析不成字面量的主机同理。全程不做 DNS 解析。
    """

    candidate = host.strip().rstrip(".").lower()
    if not candidate:
        return True
    if candidate == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if _traditional_ipv4(candidate):
            return True
        if any(
            not (character.isascii() and (character.isalnum() or character in ".-"))
            for character in candidate
        ):
            return True
        return not any(character.isascii() and character.isalpha() for character in candidate)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_local_or_private_host(str(address.ipv4_mapped))
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _traditional_ipv4(host: str) -> bool:
    """主机是否为 `inet_aton` 能解析、但不是规范点分四段的传统 IPv4 写法。

    只用于**识别**歧义写法（不把它归一后放行，也不做 DNS 查询）：这类写法在
    不同解析器下可能指向不同地址，必须按不安全拒绝。
    """

    try:
        canonical = socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, ValueError):
        return False
    return canonical != host


def source_outbound_segments(
    material: SourceMaterial,
) -> tuple[dict[str, Any], ...] | PrepareRejected:
    """把已准备材料转成 OneBot 发送段（`text` 与资源引用 `forward`）。

    发送段由本函数**重建**：卡片只带 `data.id`，普通材料只带已核实的段形态，
    材料里可能夹带的额外字段（本机路径、`file`、未展开节点等）一律不进入发送。
    """

    if material.kind == "forward":
        resource_id = verbatim_identifier(material.forward_resource_id)
        if not resource_id:
            return PrepareRejected(
                ERROR_SOURCE_RESOURCE_MISSING,
                "卡片资源引用不是安全可引用的标识符，未进入平台调用",
            )
        return ({"type": "forward", "data": {"id": resource_id}},)

    segments: list[dict[str, Any]] = []
    for index, segment in enumerate(material.segments, start=1):
        if not isinstance(segment, Mapping):
            return PrepareRejected(
                ERROR_SOURCE_SEGMENT_UNSUPPORTED,
                f"材料第 {index} 段结构无效，未进入平台调用",
            )
        segment_type = str(segment.get("type") or "").strip()
        if segment_type == "text":
            text = _text_payload(segment)
            if text is None:
                return PrepareRejected(
                    ERROR_SOURCE_SEGMENT_UNSUPPORTED,
                    f"材料第 {index} 段文本结构无效，未进入平台调用",
                )
            segments.append({"type": "text", "data": {"text": text}})
            continue
        if segment_type == "image":
            image = _image_segment(segment)
            if image is None:
                return PrepareRejected(
                    ERROR_SOURCE_MEDIA_UNSAFE,
                    f"材料第 {index} 段图片媒体引用不可安全重放，未进入平台调用",
                )
            segments.append(image)
            continue
        return PrepareRejected(
            ERROR_SOURCE_SEGMENT_UNSUPPORTED,
            f"材料第 {index} 段类型 {_segment_label(segment_type)} 无法完整发送，未进入平台调用",
        )
    if not segments:
        return PrepareRejected(
            ERROR_SOURCE_SEGMENT_UNSUPPORTED, "材料没有可发送的内容段，未进入平台调用"
        )
    return tuple(segments)


def _card_material(segment: Mapping[str, Any]) -> SourceMaterial | PrepareRejected:
    """从卡片段取唯一资源引用；空或非法资源引用整条拒绝。"""

    data = segment.get("data")
    raw_id = data.get("id") if isinstance(data, Mapping) else None
    if isinstance(raw_id, bool):
        raw_id = None
    if isinstance(raw_id, int):
        raw_id = str(raw_id)
    resource_id = verbatim_identifier(raw_id) if isinstance(raw_id, str) else ""
    if not resource_id:
        return PrepareRejected(
            ERROR_SOURCE_RESOURCE_MISSING,
            "源卡片缺少可用的资源引用（不接受源消息 ID 代替），整条拒绝",
        )
    return SourceMaterial(kind="forward", forward_resource_id=resource_id)


def _text_payload(segment: Mapping[str, Any]) -> str | None:
    """OneBot 文本段的内容：`{"type": "text", "data": {"text": ...}}`（1.3 核实）。"""

    data = segment.get("data")
    if not isinstance(data, Mapping):
        return None
    text = data.get("text")
    return text if isinstance(text, str) else None


def _segments_kind(segments: list[dict[str, Any]]) -> SourceKind:
    """按段类型组合给出材料种类：纯文本、纯图片或混合。"""

    kinds = {str(segment.get("type") or "") for segment in segments}
    if kinds == {"text"}:
        return "text"
    if kinds == {"image"}:
        return "image"
    return "mixed"


def _segment_label(segment_type: str) -> str:
    """可安全回显的段类型标签：只回显短小的 ASCII 词法，其余显示「未知」。"""

    if not segment_type or len(segment_type) > 16:
        return "未知"
    safe = all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in segment_type
    )
    return segment_type if safe else "未知"
