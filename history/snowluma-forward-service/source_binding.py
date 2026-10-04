"""`forward_message` 的参数面、拒绝结果与一次性可信来源绑定。

工具调用的会话上下文不可信：宿主构造工具参数时先放模型参数、再对缺键补宿主
上下文，且上下文字段里没有账号（第 1 组核实 `component_query.py:929-989`、
`:740-773`）。因此授权不能读取工具调用里的平台、会话或任何内部字段，只能在宿主
构造、模型不可写的 `maisaka.planner.after_response` Hook 载荷上签发一次性凭据：
凭据绑定真实 Planner 会话与源消息 ID，随工具调用携带，首次消费即作废。

本模块负责公开参数声明与校验、拒绝结果的统一措辞与凭据表；工具入口的编排见
`forward_precheck.py`。
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any, Mapping

PUBLIC_FORWARD_ARGUMENTS = ("message_id", "target_group_id", "comment")
"""`forward_message` 的公开参数（spec 三参数契约）。"""

REQUIRED_FORWARD_ARGUMENTS = PUBLIC_FORWARD_ARGUMENTS[:2]
"""必填的公开参数（消息 ID 与目标群）；comment 可选、默认空字符串。"""

SOURCE_TOKEN_FIELD = "_courier_source_token"
"""内部一次性凭据字段：仅由 `maisaka.planner.after_response` Hook 写入。"""

FORWARD_TOOL_NAME = "forward_message"
"""本插件公开工具名：清洗与签发同样只作用于指向该工具的调用。"""

FORWARD_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message_id": {
            "type": "string",
            "description": "本群源消息的消息 ID（字符串，避免长 ID 精度损失）。",
        },
        "target_group_id": {
            "type": "string",
            "description": "接收转发的目标群号（必须在允许的目标群内，且不同于源群）。",
        },
        "comment": {
            "type": "string",
            "default": "",
            "description": "可选评论，按普通文本发送；留空表示不附带评论。",
        },
    },
    "required": list(REQUIRED_FORWARD_ARGUMENTS),
}
"""工具参数 JSON Schema：只有三个公开字符串参数，模型无法声明会话或授权字段。"""

FUNCTION_CALL_ITEM_TYPE = "FunctionCallItem"
"""宿主 `maisaka.planner.after_response` 载荷中函数调用 Item 的类型标记。"""

MAX_PENDING_BINDINGS = 1024
"""待消费凭据上限；超出后淘汰最旧项，避免凭据表无限增长。"""

NOTHING_SENT_NOTE = "本次未发送任何消息"
"""所有拒绝结果的统一定尾：明确本次调用没有任何发送副作用。"""


@dataclass(frozen=True, slots=True)
class Rejected:
    """安全拒绝：只带可定位的中文原因，不含越权内容或凭证。"""

    reason: str

    @classmethod
    def not_sent(cls, reason: str) -> "Rejected":
        """按统一定尾构造拒绝结果（原因已带定尾时不重复叠加）。"""

        body = reason.rstrip("。，,;； \t")
        if body.endswith(NOTHING_SENT_NOTE):
            return cls(body)
        return cls(f"{body}，{NOTHING_SENT_NOTE}")


@dataclass(frozen=True, slots=True)
class ForwardArguments:
    """已校验的三个公开调用参数。"""

    message_id: str
    target_group_id: str
    comment: str


def parse_forward_arguments(arguments: Mapping[str, Any]) -> ForwardArguments | Rejected:
    """按 `REQUIRED_FORWARD_ARGUMENTS` 驱动校验公开参数，返回 `ForwardArguments`。

    ID 一律要求字符串并只做首尾空白归一，绝不做数值转换（长数字 ID 转 float 会
    丢失精度）；缺失、空白或类型不符一律返回具体原因。
    """

    parsed: dict[str, str] = {}
    for name in REQUIRED_FORWARD_ARGUMENTS:
        value = _required_id(arguments, name)
        if isinstance(value, Rejected):
            return value
        parsed[name] = value

    comment = _optional_comment(arguments)
    if isinstance(comment, Rejected):
        return comment
    return ForwardArguments(
        message_id=parsed["message_id"],
        target_group_id=parsed["target_group_id"],
        comment=comment,
    )


def _required_id(arguments: Mapping[str, Any], name: str) -> str | Rejected:
    """校验必填字符串 ID。"""

    if name not in arguments or arguments.get(name) is None:
        return Rejected.not_sent(f"缺少必填参数 {name}")
    value = arguments.get(name)
    if not isinstance(value, str):
        return Rejected.not_sent(f"参数 {name} 必须是字符串，收到 {type(value).__name__}")
    normalized = value.strip()
    if not normalized:
        return Rejected.not_sent(f"参数 {name} 不能为空")
    return normalized


def _optional_comment(arguments: Mapping[str, Any]) -> str | Rejected:
    """校验可选评论：只接受字符串，空字符串代表不附带评论。"""

    if "comment" not in arguments:
        return ""
    value = arguments.get("comment")
    if not isinstance(value, str):
        return Rejected.not_sent(
            f"参数 comment 必须是字符串（不附带评论请传空字符串），"
            f"收到 {type(value).__name__}"
        )
    return value


class SourceBindingTable:
    """一次性来源凭据表：签发、清洗与消费。

    只接受 `output_items` 形态（宿主在已核实版本里必传该字段，见
    `chat_loop_service.py:1171`）；缺失或结构异常时不签发，工具入口按缺少授权
    安全拒绝。
    """

    def __init__(self) -> None:
        self._bindings: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def sanitize_and_bind_output_items(self, session_id: str, output_items: Any) -> Any:
        """清洗本轮输出项里的本插件工具调用，并为它签发新凭据。

        宿主把模型输出当**一批** `tool_calls` 处理（`reasoning_engine.py:2099-…`
        的 `_handle_tool_calls`），同一载荷里混有指向其它工具的调用：那些调用必须
        原样保留，也不能被签发凭据或被白名单改写参数。因此这里只处理
        `func_name` 为本插件 `forward_message` 的调用；公开参数原样保留。
        """

        if not isinstance(output_items, list):
            return output_items
        normalized_session_id = str(session_id or "").strip()
        sanitized_items = deepcopy(output_items)
        for item in sanitized_items:
            if not isinstance(item, dict) or item.get("item_type") != FUNCTION_CALL_ITEM_TYPE:
                continue
            tool_call = item.get("tool_call")
            if not isinstance(tool_call, dict):
                continue
            if str(tool_call.get("func_name") or "") != FORWARD_TOOL_NAME:
                continue
            arguments = tool_call.get("args")
            if not isinstance(arguments, dict):
                continue
            sanitized_arguments = {
                key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments
            }
            message_id = arguments.get("message_id")
            if normalized_session_id and isinstance(message_id, str) and message_id.strip():
                sanitized_arguments[SOURCE_TOKEN_FIELD] = self._issue(
                    normalized_session_id,
                    message_id.strip(),
                )
            tool_call["args"] = sanitized_arguments
        return sanitized_items

    def consume(self, message_id: str, arguments: Mapping[str, Any]) -> str | None:
        """消费一次性凭据并返回绑定的真实会话 ID；缺失、未知或错配返回 `None`。"""

        token = str(arguments.get(SOURCE_TOKEN_FIELD) or "").strip()
        if not token:
            return None
        authorization = self._bindings.pop(token, None)
        if authorization is None:
            return None
        session_id, authorized_message_id = authorization
        if authorized_message_id != str(message_id or "").strip():
            return None
        return session_id

    def _issue(self, session_id: str, message_id: str) -> str:
        """登记一条有容量上限的一次性凭据，返回不可预测的随机值。"""

        token = token_urlsafe(32)
        self._bindings[token] = (session_id, message_id)
        while len(self._bindings) > MAX_PENDING_BINDINGS:
            self._bindings.popitem(last=False)
        return token
