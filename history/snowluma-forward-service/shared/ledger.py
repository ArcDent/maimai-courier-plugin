"""服务端账本仓储的公开协议（design D7）。

第 3 组实现 SQLite 仓储；本模块只定义公开签名、快照与封闭状态，不碰 SQL、不打开
文件、不做事务、不连网。

- 唯一投递身份是 `DeliveryKey` 四字段；`delivery_id` 只是它的稳定摘要，用于对外
  查询与撤销，**不是**防重键；
- 尝试（attempt）记录一次客户端请求的源/评论阶段状态；重放旧请求只返回旧结果，
  不会开启新尝试；
- 「记录不存在」返回 `None`；存储失败必须抛 `LedgerUnavailableError`
  （`shared.delivery`），两者不得混同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, get_args, runtime_checkable

from .delivery import DeliveryKey, LedgerStatus, SendStatus
from .protocol import CommentStatus

AttemptStage = Literal["prepared", "sending", "sent", "failed", "unknown", "cancelled"]
"""尝试的源阶段：已准备（未授权）/ 在途 / 已发送 / 明确未发 / 待核实 / 已撤销。"""

ATTEMPT_STAGE_VALUES: frozenset[str] = frozenset(get_args(AttemptStage))


@dataclass(frozen=True, slots=True)
class DeliverySnapshot:
    """某投递键的当前阻断事实与真实回执（源权威状态 + 评论结果）。"""

    delivery_id: str
    key: DeliveryKey
    status: LedgerStatus
    target_message_id: str = ""
    comment_status: CommentStatus = "not_requested"
    comment_message_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    """一次尝试的公开快照：请求/会话/代次定位 + 源/评论阶段状态与已知回执。

    `key` 是尝试所属的投递四字段身份（账本行本就保存这四列）：协调器据此在**没有**
    投递表记录时也能解析投递身份（未占位的准备阶段只有尝试行），不需要第二份定义。
    """

    attempt_id: str
    delivery_id: str
    request_id: str
    session_id: str
    policy_generation: int
    source_stage: AttemptStage
    comment_stage: CommentStatus
    comment: str = ""
    target_message_id: str = ""
    comment_message_id: str = ""
    reason: str = ""
    key: DeliveryKey | None = None


@dataclass(frozen=True, slots=True)
class SourceReservation:
    """源发送占位结果：`granted` 为真表示本次调用获得发送资格。"""

    granted: bool
    snapshot: DeliverySnapshot


@dataclass(frozen=True, slots=True)
class CommentReservation:
    """评论发送占位结果：`granted` 为真表示本次调用获得评论发送资格。"""

    granted: bool
    snapshot: DeliverySnapshot


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """启动恢复统计：源/评论在途占位归并为待核实的条数，以及失效的准备数。"""

    sources_recovered: int = 0
    comments_recovered: int = 0
    prepares_expired: int = 0


@runtime_checkable
class LedgerRepository(Protocol):
    """服务端账本仓储：投递、尝试、源/评论占位与结果查询。

    全部方法都是 `async`：服务端协调器在事件循环里 `await` 它们（实现内部可以是
    SQLite 短操作，但**绝不在持有事务时等待网络**）。实现者必须保证：

    - `reserve_source` / `reserve_comment` 原子决定至多一个合法发送尝试：既有
      `sent` / `sending` / `unknown` 一律不授予，只有「明确未发送」的旧尝试才允许
      新的合法尝试；
    - 成功只接受非空真实目标消息 ID；评论结果附属于源记录，不覆盖源 ID；
    - `recover_interrupted` 把遗留的源/评论在途占位归并为待核实，不自动续发。
    """

    async def identity(self) -> str:
        """账本身份：初始化时生成、跨重启稳定（误连空库靠它识别）。"""

        ...

    async def prepare_attempt(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        request_id: str,
        session_id: str,
        policy_generation: int,
        comment: str = "",
    ) -> AttemptSnapshot | None:
        """登记一次「已准备」尝试（只记录定位与本次评论，不产生发送事实）。

        同一尝试 ID 重复准备必须幂等返回原快照；尝试 ID 已被另一个投递占用时返回
        `None`。准备成功不承诺可发送：占位时由 `reserve_source` 统一决定是否授予。
        """

        ...

    async def reserve_source(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        request_id: str,
        session_id: str,
        policy_generation: int,
    ) -> SourceReservation:
        """登记源阶段占位（`sending`）并建立本次尝试。"""

        ...

    async def finalize_source(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        status: SendStatus,
        target_message_id: str = "",
        reason: str = "",
    ) -> bool:
        """落源阶段终态：成功必须带真实目标消息 ID；返回是否命中在途占位。"""

        ...

    async def reserve_comment(self, key: DeliveryKey, *, attempt_id: str) -> CommentReservation:
        """在源已确认成功且未落账之前不授予；同一尝试至多一次评论资格。"""

        ...

    async def finalize_comment(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        status: CommentStatus,
        comment_message_id: str = "",
        reason: str = "",
    ) -> bool:
        """保存评论结果（不覆盖源目标 ID）；返回是否命中评论占位。"""

        ...

    async def cancel_attempt(self, key: DeliveryKey, *, attempt_id: str, reason: str) -> bool:
        """撤销一次未消费的准备：源阶段 `prepared` → `cancelled`、评论 `pending` → `skipped`。

        只命中「该尝试属于该键且源阶段仍为 `prepared`」的未消费准备：已开始的阶段不在此列
        （它们的真实结果由 `finalize_source` / `finalize_comment` 登记）。返回是否命中。
        """

        ...

    async def lookup_delivery(self, delivery_id: str) -> DeliverySnapshot | None:
        """按投递 ID 读取权威状态；不存在返回 `None`（不是空历史）。"""

        ...

    async def lookup_attempt(self, attempt_id: str) -> AttemptSnapshot | None:
        """按尝试 ID 读取原执行结果；不存在返回 `None`。"""

        ...

    async def recover_interrupted(self) -> RecoveryReport:
        """启动恢复：源/评论在途占位归并待核实、未提交准备失效。"""

        ...

    async def close(self) -> None:
        """释放仓储资源（不删除任何投递事实）。"""

        ...
