"""共享投递结果面：状态词汇、结果类型与唯一的结果渲染。

结果状态与渲染口径只有这一份定义，编排与客户端都从这里导入。本模块不依赖
插件侧模块：材料只按结构面（`kind` + `shape`）渲染，不导入 `send_material`，
因此无 `maibot_sdk` 的解释器可以直接导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, Union

from .delivery import LedgerStatus, SendStatus

ToolStatus = Union[
    LedgerStatus,
    Literal["partial", "already_forwarded", "rejected", "ledger_unavailable"],
]
"""工具结果状态面：账本四状态 + 「部分成功」+「已转发」+「拒绝」+「账本不可用」。"""

TOOL_STATUS_SENT: Final[ToolStatus] = "sent"
"""本次调用已确认源消息投递成功（评论未要求或已成功）。"""

TOOL_STATUS_PARTIAL: Final[ToolStatus] = "partial"
"""部分成功：源消息已确认投递并落账，但评论明确未发送或结果待核实。"""

TOOL_STATUS_ALREADY_FORWARDED: Final[ToolStatus] = "already_forwarded"
"""同键此前已成功投递：本次未产生任何新发送。"""

TOOL_STATUS_SENDING: Final[ToolStatus] = "sending"
"""同键投递在途（并发调用只会发送一次）。"""

TOOL_STATUS_REJECTED: Final[ToolStatus] = "rejected"
"""预检或总开关拒绝：本次未发送任何消息。"""

TOOL_STATUS_UNKNOWN: Final[ToolStatus] = "unknown"
"""结果待核实：可能已发送，必须阻止重发。"""

TOOL_STATUS_FAILED: Final[ToolStatus] = "failed"
"""明确未发送的失败：允许后续显式重试。"""

TOOL_STATUS_LEDGER_UNAVAILABLE: Final[ToolStatus] = "ledger_unavailable"
"""投递账本不可用：已关闭发送，本次未发送任何消息。"""


@dataclass(frozen=True, slots=True)
class SourceSendResult:
    """源消息与评论共用的发送结果面。

    - `sent`：拿到真实目标消息 ID（`target_message_id` 必须非空）；
    - `failed`：明确没有发出任何消息；
    - `unknown`：超时、取消、回执丢失等无法确认的结果（调用方按待核实处理）。
    """

    status: SendStatus
    target_message_id: str = ""
    reason: str = ""


ErrorStage = Literal["", "rejected", "source_send", "source_persist", "comment_send", "comment_persist"]
"""错误阶段：拒绝 / 源发送 / 源持久化 / 评论发送 / 评论持久化（空串表示没有故障）。"""

STAGE_NONE: Final[ErrorStage] = ""
"""没有故障（完整成功、已转发、发送中等信息性结果）。"""

STAGE_REJECTED: Final[ErrorStage] = "rejected"
"""预检或总开关拒绝：本次未发送任何消息。"""

STAGE_SOURCE_SEND: Final[ErrorStage] = "source_send"
"""源消息发送阶段：明确未发送或结果待核实。"""

STAGE_SOURCE_PERSIST: Final[ErrorStage] = "source_persist"
"""源消息状态持久化阶段：占位或回执落盘失败。"""

STAGE_COMMENT_SEND: Final[ErrorStage] = "comment_send"
"""评论发送阶段：明确未发送或结果待核实。"""

STAGE_COMMENT_PERSIST: Final[ErrorStage] = "comment_persist"
"""评论结果持久化阶段：评论结果未能落盘。"""

COMMENT_DELAY_SECONDS: Final[float] = 0.5
"""评论固定延迟（秒）：代码常量，不是配置项，也不接受随机时长或模型参数覆盖。"""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """投递结果：供工具入口组装结果字典。

    `sent` 表示源消息**已确认投递**（本次或既有成功记录）；`delivered` 是其派生
    视图（同一语义，避免两个字段恒等重复）；`already_forwarded` 表示本次没有产生
    新的发送；`target_message_id` 是已取得的真实目标消息 ID；`comment_status` 是
    本记录上已保存的评论结果。
    """

    status: ToolStatus
    sent: bool = False
    already_forwarded: bool = False
    target_message_id: str = ""
    comment_status: str = ""
    comment_message_id: str = ""
    stage: ErrorStage = STAGE_NONE
    reason: str = ""

    @property
    def delivered(self) -> bool:
        """源消息是否已确认投递（本次或既有成功记录）；与 `sent` 同语义。"""

        return self.sent


class MaterialSummary(Protocol):
    """材料摘要的结构面：种类 + 结构指纹（只有段类型与嵌套、不含正文）。

    `send_material.SendMaterial` 天然满足本协议；渲染只读这两个属性，不导入插件侧
    模块，也不复制材料正文。
    """

    @property
    def kind(self) -> str: ...

    @property
    def shape(self) -> tuple[Any, ...]: ...


def render_forward_result(
    result: DeliveryResult,
    *,
    material: MaterialSummary | None = None,
    source_message_id: str = "",
    target_group_id: str = "",
) -> dict[str, Any]:
    """把投递结果组装成统一工具结果面（唯一的结果口径）。

    `success` 只在完整成功时为 true（`sent` / `already_forwarded`）；部分成功、
    待核实与已知回执但未落盘都不是完整成功。`sent` / `delivered` 只表示源消息
    已确认投递（含既有成功记录）。`material` 只在预检通过后传入：拒绝分支没有
    已准备的材料，不得声称材料完整。
    """

    rendered: dict[str, Any] = {
        "success": result.status in (TOOL_STATUS_SENT, TOOL_STATUS_ALREADY_FORWARDED),
        "status": result.status,
        "sent": result.sent,
        "delivered": result.delivered,
        "already_forwarded": result.already_forwarded,
        "target_message_id": result.target_message_id,
        "comment_status": result.comment_status,
        "comment_message_id": result.comment_message_id,
        "source_message_id": source_message_id,
        "target_group_id": target_group_id,
        "stage": result.stage,
        "reason": result.reason,
    }
    if material is not None:
        rendered["send_kind"] = material.kind
        rendered["material_shape"] = list(material.shape)
    return rendered
