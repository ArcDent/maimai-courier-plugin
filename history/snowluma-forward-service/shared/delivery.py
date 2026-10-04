"""共享投递身份与账本状态词汇：账号 + 源群 + 源消息 ID + 目标群（design D7）。

本模块是 `DeliveryKey`、`LedgerStatus`（含 `SendStatus` 别名）与账本错误类型的
**唯一定义处**：插件侧 `delivery_ledger.py` 与服务端账本（第 3 组）都从这里重导出
或导入，不复制第二份声明（design D9）。

投递身份只有四字段——评论、请求 ID、尝试 ID、进程 ID 都不参与；源群与目标群是
两个独立字段，任何一方都不得被另一方的值填充。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

LedgerStatus = Literal["sending", "sent", "failed", "unknown"]
"""投递记录状态（账本与结果面共用的唯一状态类型声明）。"""

SendStatus = LedgerStatus
"""发送步骤的结果值域：与账本状态共用**同一类型声明**，避免两份字面量漂移。

发送步骤（源消息与评论）的合法终态只有 `sent` / `failed` / `unknown`；`sending`
在发送步骤里不是合法取值，编排按契约外取值兜底为待核实，不会降级为可重试失败。
"""

STATUS_SENDING: Final[LedgerStatus] = "sending"
"""已持久占位：发送已开始但尚无确认结果。"""

STATUS_SENT: Final[LedgerStatus] = "sent"
"""已确认投递成功（带真实目标消息 ID）。"""

STATUS_FAILED: Final[LedgerStatus] = "failed"
"""明确未发送的失败：允许后续显式调用重试。"""

STATUS_UNKNOWN: Final[LedgerStatus] = "unknown"
"""结果待核实：可能已发送，必须阻止自动/后续重发。"""


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    """投递唯一键：账号 + 源群 + 源消息 ID + 目标群。"""

    account_id: str
    source_group_id: str
    source_message_id: str
    target_group_id: str


@dataclass(frozen=True, slots=True)
class LedgerUnavailable:
    """账本不可用：带明确中文原因；调用方必须关闭发送并如实报告。"""

    reason: str


class LedgerUnavailableError(RuntimeError):
    """账本存储操作失败（读失败、写失败或恢复失败），与「记录不存在」区分。

    只携带可定位的中文原因（`_describe` 口径），不含 SQL、路径或用户数据；
    调用方必须按「未能落盘 / 无法读取」处理，不得当作空结果。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
