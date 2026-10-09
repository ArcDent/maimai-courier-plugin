"""共享投递身份与账本状态词汇：账号 + 源群 + 源消息 ID + 目标 + 目标形态。

`DeliveryKey` / `TargetKind` / `LedgerStatus`（含 `SendStatus` 别名）与账本错误
类型在本模块唯一定义，账本、编排与客户端都从这里导入。投递身份只有五字段，评论、
请求 ID、进程 ID 等都不参与；目标形态是身份的一部分——群号与好友 QQ 号是两个
重叠的数字空间，不带形态就无法把「发往群 12345」与「发往好友 12345」分成两笔
互不阻断的投递（ADR 0012）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

LedgerStatus = Literal["sending", "sent", "failed", "unknown"]
"""投递记录状态（账本与结果面共用的唯一状态类型声明）。"""

SendStatus = LedgerStatus
"""发送步骤的结果值域：与账本状态共用同一类型声明，避免两份字面量漂移。

合法终态只有 `sent` / `failed` / `unknown`；`sending` 不是合法取值，编排按契约外
取值兜底为待核实，不降级为可重试失败。
"""

STATUS_SENDING: Final[LedgerStatus] = "sending"
"""已持久占位：发送已开始但尚无确认结果。"""

STATUS_SENT: Final[LedgerStatus] = "sent"
"""已确认投递成功（带真实目标消息 ID）。"""

STATUS_FAILED: Final[LedgerStatus] = "failed"
"""明确未发送的失败：允许后续显式调用重试。"""

STATUS_UNKNOWN: Final[LedgerStatus] = "unknown"
"""结果待核实：可能已发送，必须阻止自动/后续重发。"""

TargetKind = Literal["group", "private"]
"""投递目标形态（只有两个取值）：群目标或私聊目标。

取值字面量与常量在本模块单点定义：账本键、发送请求路由与结果渲染都从这里导入。
"""

TARGET_KIND_GROUP: Final[TargetKind] = "group"
"""群目标：`target_group_id` 存目标群号。"""

TARGET_KIND_PRIVATE: Final[TargetKind] = "private"
"""私聊目标：`target_group_id` 存对端 QQ 号。"""

TARGET_KIND_VALUES: Final[frozenset[str]] = frozenset(get_args(TargetKind))
"""目标形态的封闭取值集合：配置与判定只认这两个字面量。"""


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    """投递唯一键：账号 + 源群 + 源消息 ID + 目标 + 目标形态。

    `target_group_id` 在两种形态下都存目标标识（群号或对端 QQ 号），含义由
    `target_kind` 决定；`target_kind` 没有默认值：忘记传形态必须立刻失败，不允许
    静默按群目标落账（ADR 0012）。
    """

    account_id: str
    source_group_id: str
    source_message_id: str
    target_group_id: str
    target_kind: TargetKind


@dataclass(frozen=True, slots=True)
class LedgerUnavailable:
    """账本不可用：带明确中文原因；调用方必须关闭发送并如实报告。"""

    reason: str


class LedgerUnavailableError(RuntimeError):
    """账本存储操作失败（读失败、写失败或恢复失败），与「记录不存在」区分。

    只携带可定位的中文原因，不含 SQL、路径或用户数据；调用方必须按「未能落盘 /
    无法读取」处理，不得当作空结果。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
