"""`forward_message` / `list_forward_friends` 的参数面、拒绝结果与一次性可信来源绑定。

工具调用的会话上下文不可信：宿主构造工具参数时先放模型参数、再对缺键补宿主上下文，
且上下文字段里没有账号。因此授权不能读工具调用里的平台、会话或任何内部字段，只能在
宿主构造、模型不可写的 `maisaka.planner.after_response` Hook 载荷上签发一次性凭据：
绑定真实 Planner 会话与源消息 ID，随工具调用携带，首次消费即作废。工具入口的编排见
`forward_precheck.py`（转发）与 `plugin.py`（名册）。
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any, Mapping

from .shared.delivery import TARGET_KIND_GROUP, TARGET_KIND_PRIVATE, TargetKind

PUBLIC_FORWARD_ARGUMENTS = ("message_id", "target_group_id", "target_user_id", "comment")
"""`forward_message` 的公开参数（双形态目标契约）。

`target_group_id`（群目标）与 `target_user_id`（私聊目标）二选一：同缺或同在都在
解析时拒绝，不默认选择任一目标；`comment` 可选、默认空字符串。
"""

REQUIRED_FORWARD_ARGUMENTS = ("message_id",)
"""必填的公开参数（只有消息 ID）。

目标参数不走必填清单：二选一互斥由 `parse_forward_arguments` 显式判定，避免
「必填其一」被误读成「两者都要」。
"""

SOURCE_TOKEN_FIELD = "_courier_source_token"
"""内部一次性凭据字段：仅由 `maisaka.planner.after_response` Hook 写入。"""

FORWARD_TOOL_NAME = "forward_message"
"""本插件公开工具名：清洗与签发同样只作用于指向该工具的调用。"""

LIST_FORWARD_FRIENDS_TOOL_NAME = "list_forward_friends"
"""本插件只读名册工具名：与转发工具共用同一条一次性来源凭据链（design D2/D10）。"""

FORWARD_TOOL_NAMES: tuple[str, ...] = (FORWARD_TOOL_NAME, LIST_FORWARD_FRIENDS_TOOL_NAME)
"""本插件全部公开工具名：凭据清洗与群聊作用域声明都按本清单单点判定（design D10）。"""

ROSTER_BINDING_MESSAGE_ID = ""
"""名册工具凭据绑定的「消息 ID」：名册工具没有公开参数，凭据只绑定会话。

转发工具的凭据绑定非空消息 ID，名册工具的凭据绑定空串，因此两类凭据不能跨工具
消费（`consume` 按消息 ID 比对，错配即拒）。本哨兵只由 `SourceBindingTable`
内部消费（`consume_roster`），不泄漏到组合根。
"""


@dataclass(frozen=True, slots=True)
class ToolBindingProfile:
    """工具的公开参数面与凭据绑定形态（按工具名显式登记，design D10）。

    `public_arguments` 是清洗后保留的公开参数名（名册工具没有任何公开参数，模型
    自报字段全部丢弃）；`bound_message_id` 是凭据绑定的消息 ID：固定值表示只绑
    会话（名册工具），`None` 表示按本次调用的 `message_id` 动态绑定（转发工具）。
    两个字段都没有默认值：新工具必须在 `TOOL_BINDING_PROFILES` 里逐项写明自己的
    形态，不会因为落在默认分支而静默继承任何一种绑定语义。
    """

    public_arguments: tuple[str, ...]
    bound_message_id: str | None


TOOL_BINDING_PROFILES: dict[str, ToolBindingProfile] = {
    LIST_FORWARD_FRIENDS_TOOL_NAME: ToolBindingProfile(
        public_arguments=(), bound_message_id=ROSTER_BINDING_MESSAGE_ID),
    FORWARD_TOOL_NAME: ToolBindingProfile(
        public_arguments=PUBLIC_FORWARD_ARGUMENTS, bound_message_id=None),
}
"""「工具名 → (公开参数, 绑定消息 ID)」小表：键必须覆盖 `FORWARD_TOOL_NAMES` 全部工具。

清洗与签发按本表显式分派：表外工具不签发凭据、也不保留任何参数。新增公开工具
必须在这里登记自己的参数面与绑定形态，从形式上保证将来的第三个工具不会静默获得
「按 message_id 绑定」的语义。
"""

FORWARD_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message_id": {
            "type": "string",
            "description": "本群源消息的消息 ID（字符串，避免长 ID 精度损失）。",
        },
        "target_group_id": {
            "type": "string",
            "description": (
                "接收转发的目标群号（必须与来源群属于同一转发分组，且不同于源群）；"
                "与 target_user_id 二选一，两者恰好提供一个。"
            ),
        },
        "target_user_id": {
            "type": "string",
            "description": (
                "私聊目标的对端 QQ 号（须为当前账号好友，且来源群所属分组开启私聊开关）；"
                "与 target_group_id 二选一，两者恰好提供一个。"
            ),
        },
        "comment": {
            "type": "string",
            "default": "",
            "description": "可选评论，按普通文本发送；留空表示不附带评论。",
        },
    },
    "required": list(REQUIRED_FORWARD_ARGUMENTS),
}
"""工具参数 JSON Schema：模型无法声明会话、平台、账号或授权字段。"""

FRIEND_LIST_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}
"""名册工具参数 JSON Schema：没有任何公开参数。

名册查询不需要模型提供任何输入（取哪个账号的名册由插件按可信来源与端点判定），
模型自报的字段既不被读取、也在清洗时全部丢弃，从形式上杜绝「模型伪造会话/平台/
账号/授权字段」的入口（与转发工具同一红线，design D2/D10）。
"""

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
    """已校验的公开调用参数。

    目标按形态分流，且同一时刻只有一个目标字段非空（参数解析时已判二选一）：
    `target_group_id` 存群目标群号（私聊目标为空串），`target_user_id` 存私聊目标的
    对端 QQ 号（群目标为空串）。`target_kind` 由哪个目标字段非空派生，不与两个目标
    字段手工同步；群号与好友 QQ 号是两个重叠的数字空间，读侧按形态分别核验
    （ADR 0012）。
    """

    message_id: str
    target_group_id: str = ""
    target_user_id: str = ""
    comment: str = ""

    @property
    def target_kind(self) -> TargetKind:
        """目标形态（`group` / `private`）：由哪个目标字段非空单点派生。"""

        return TARGET_KIND_PRIVATE if self.target_user_id.strip() else TARGET_KIND_GROUP


def parse_forward_arguments(arguments: Mapping[str, Any]) -> ForwardArguments | Rejected:
    """校验公开参数，返回 `ForwardArguments`。

    `message_id` 必填；两个目标参数二选一（同缺/同在均拒绝，不默认选群也不默认选
    私聊）。ID 一律要求字符串并只做首尾空白归一，绝不做数值转换（长数字 ID 转
    float 会丢失精度）。
    """

    message_id = _required_id(arguments, "message_id")
    if isinstance(message_id, Rejected):
        return message_id
    target_group_id = _optional_id(arguments, "target_group_id")
    if isinstance(target_group_id, Rejected):
        return target_group_id
    target_user_id = _optional_id(arguments, "target_user_id")
    if isinstance(target_user_id, Rejected):
        return target_user_id
    if bool(target_group_id) == bool(target_user_id):
        return Rejected.not_sent(
            "target_group_id 与 target_user_id 必须恰好提供一个（二选一），不默认选择任一目标"
        )

    comment = _optional_comment(arguments)
    if isinstance(comment, Rejected):
        return comment
    return ForwardArguments(
        message_id=message_id,
        target_group_id=target_group_id,
        target_user_id=target_user_id,
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


def _optional_id(arguments: Mapping[str, Any], name: str) -> str | Rejected:
    """校验二选一目标参数：缺失、`None` 或全空白按未提供处理；非字符串拒绝。

    空白目标不构成「提供了一个目标」，与「恰好提供一个」的口径一致（同缺/同在的
    拒绝都在 `parse_forward_arguments` 显式判定）。
    """

    if name not in arguments or arguments.get(name) is None:
        return ""
    value = arguments.get(name)
    if not isinstance(value, str):
        return Rejected.not_sent(f"参数 {name} 必须是字符串，收到 {type(value).__name__}")
    return value.strip()


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

        宿主把模型输出当**一批** `tool_calls` 处理，同一载荷里混有指向其它工具的
        调用：那些调用必须原样保留，也不能被签发凭据或被改写参数。因此这里只处理
        `func_name` 在本插件工具清单（`FORWARD_TOOL_NAMES`）内的调用；公开参数按
        工具拆分（design D10：两个工具互不复用字段集，名册工具没有任何公开参数，
        模型自报字段在清洗时全部丢弃）。
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
            tool_name = str(tool_call.get("func_name") or "")
            if tool_name not in FORWARD_TOOL_NAMES:
                continue
            arguments = tool_call.get("args")
            if not isinstance(arguments, dict):
                continue
            tool_call["args"] = self._sanitized_arguments(
                normalized_session_id,
                tool_name,
                arguments,
            )
        return sanitized_items

    def _sanitized_arguments(
        self, session_id: str, tool_name: str, arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """按工具显式登记的参数面清洗并签发一次性凭据（表外工具不签发）。

        分发只认 `TOOL_BINDING_PROFILES`：名册工具只绑会话（无公开参数），转发工具
        另绑本次 `message_id`（缺失或非字符串都不签发）；表外工具不落任何默认分支，
        直接返回空参数面。
        """

        profile = TOOL_BINDING_PROFILES.get(tool_name)
        if profile is None:
            return {}
        sanitized: dict[str, Any] = {
            key: deepcopy(arguments[key]) for key in profile.public_arguments if key in arguments
        }
        bound_message_id = profile.bound_message_id
        if bound_message_id is None:
            # 转发工具：凭据另绑本次 message_id，缺失或非字符串都不签发。
            message_id = arguments.get("message_id")
            bound_message_id = message_id.strip() if isinstance(message_id, str) else ""
            if not bound_message_id:
                return sanitized
        if session_id:
            sanitized[SOURCE_TOKEN_FIELD] = self._issue(session_id, bound_message_id)
        return sanitized

    def consume(self, message_id: str, arguments: Mapping[str, Any]) -> str | None:
        """消费一次性凭据并返回绑定的真实会话 ID；缺失、未知或错配返回 `None`。

        转发工具传本次 `message_id` 比对，名册工具走 `consume_roster`（空串哨兵收
        在表内）。两类凭据绑定的消息 ID 形态不同，因此不能跨工具消费。
        """

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

    def consume_roster(self, arguments: Mapping[str, Any]) -> str | None:
        """消费名册工具的一次性凭据（只绑会话）并返回绑定的真实会话 ID。

        名册哨兵消息 ID（`ROSTER_BINDING_MESSAGE_ID`）收在凭据表内：调用方不需要
        知道该哨兵，也不会把转发工具的 `message_id` 绑定语义带到名册路径上。
        """

        return self.consume(ROSTER_BINDING_MESSAGE_ID, arguments)

    def _issue(self, session_id: str, message_id: str) -> str:
        """登记一条有容量上限的一次性凭据，返回不可预测的随机值。"""

        token = token_urlsafe(32)
        self._bindings[token] = (session_id, message_id)
        while len(self._bindings) > MAX_PENDING_BINDINGS:
            self._bindings.popitem(last=False)
        return token
