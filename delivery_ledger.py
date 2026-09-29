"""投递账本：宿主 `data_dir` 内的 SQLite 最小持久状态（design D6）。

- **唯一键**：机器人账号 + 源群 + 源消息 ID + 目标群；评论不参与键，评论结果作为
  字段附属于源发送记录。
- **状态**：`sending`（已占位、尚未确认）/ `sent`（已确认投递）/ `failed`（明确未
  发送，允许后续显式重试）/ `unknown`（结果待核实，阻止重发）。
- **SQL 全部参数绑定**；每次操作都是独立短事务（`BEGIN IMMEDIATE` + `COMMIT`），
  绝不在网络等待期间持有事务；BEGIN、事务体与 COMMIT 任一失败都显式回滚，连接
  不会停留在未结束事务。
- **不静默清理**：成功记录不按 TTL 或容量清除，本模块不提供任何删除接口。
- **安全失败**：库文件损坏或路径不可用时返回 `LedgerUnavailable`，调用方必须关闭
  发送；本模块不覆盖、不删除、也不把损坏库当作空库。
- **失败可区分**：读失败、写失败与恢复失败一律抛 `LedgerUnavailableError`；
  `None` 只表示「记录不存在」，`False` 只表示「没有可更新的在途占位」，两者不得
  与存储故障混同。
- **单实例假设**：`recover_interrupted` 不区分占位归属的进程，按「本进程刚打开
  账本、此时不存在本进程在途投递」判断。多个进程共享同一 `data_dir` 时，启动恢复
  会把其它活进程的 `sending` 也归并为 `unknown`（只影响可用性：该键需人工核实，
  不会造成重复发送），因此本插件按单实例（单进程）运行。

账本只管持久状态，不做发送、不做权限判断：编排见 `forward_delivery.py`。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Iterator, Literal, Union

from .clock import Clock, SystemClock

LEDGER_FILE_NAME = "delivery_ledger.sqlite3"
"""账本文件名（固定落在宿主授予的 `ctx.paths.data_dir` 下）。"""

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

_TABLE = "delivery_ledger"
"""表名（SQL 里只出现固定字面量，用户数据一律走参数绑定）。"""

LEDGER_COLUMNS = (
    "status",
    "target_message_id",
    "comment_status",
    "comment_message_id",
    "reason",
    "created_at",
    "updated_at",
)
"""`SELECT` 列顺序（与 `_fetch` 一一对应，避免 `SELECT *`）。"""


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    """投递唯一键：账号 + 源群 + 源消息 ID + 目标群。"""

    account_id: str
    source_group_id: str
    source_message_id: str
    target_group_id: str


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """投递记录快照：状态、真实目标消息 ID 与评论结果。"""

    key: DeliveryKey
    status: LedgerStatus
    target_message_id: str = ""
    comment_status: str = ""
    comment_message_id: str = ""
    reason: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


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


@dataclass(frozen=True, slots=True)
class Reservation:
    """占位结果：`granted` 为真表示本次调用获得发送资格。"""

    granted: bool
    record: DeliveryRecord


LedgerOpener = Callable[[Path], Union["DeliveryLedger", LedgerUnavailable]]
"""账本打开方式（组合根注入点：生产用 `open_ledger`，测试可注入替身）。"""


def open_ledger(path: Path, *, clock: Clock | None = None) -> DeliveryLedger | LedgerUnavailable:
    """打开（必要时创建）账本；路径不可用或库文件损坏时返回 `LedgerUnavailable`。

    只执行 `CREATE TABLE IF NOT EXISTS`：既不覆盖也不删除既有文件，损坏库不会被
    当作空库继续使用。
    """

    time_source = clock or SystemClock()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                account_id TEXT NOT NULL,
                source_group_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                target_group_id TEXT NOT NULL,
                status TEXT NOT NULL,
                target_message_id TEXT NOT NULL DEFAULT '',
                comment_status TEXT NOT NULL DEFAULT '',
                comment_message_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)
            )
            """
        )
    except (sqlite3.Error, OSError) as error:
        if connection is not None:
            connection.close()
        return LedgerUnavailable(_describe(error))
    return DeliveryLedger(connection, time_source)


def _describe(error: BaseException) -> str:
    """给出可定位、不含用户数据的失败描述（供工具结果与日志共用）。"""

    if isinstance(error, sqlite3.DatabaseError) and "not a database" in str(error):
        return "状态库文件损坏或不是 SQLite 数据库"
    if isinstance(error, sqlite3.OperationalError):
        return "状态库不可读或所在目录不可写"
    if isinstance(error, OSError):
        return "存储路径不可用"
    return "状态库访问失败"


class DeliveryLedger:
    """账本本体：短事务 + 参数绑定，由 `open_ledger` 构造。

    返回 `None` 只表示「记录不存在」，返回 `False` 只表示「没有可更新的在途占位」；
    读写与恢复的存储故障一律抛 `LedgerUnavailableError`。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock

    def reserve(self, key: DeliveryKey) -> Reservation:
        """尝试登记 `sending` 占位，原子决定本次调用是否获得发送资格。

        - 无记录或既有记录是 `failed`（明确未发送）→ 登记/重置为 `sending` 并授予；
        - 既有 `sent`（已投递）/ `sending`（在途）/ `unknown`（待核实）→ 不授予，
          返回既有记录供调用方如实汇报；
        - 存储失败抛 `LedgerUnavailableError`：调用方关闭发送（不发送）。
        """

        now = self._clock.now()
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {_TABLE} (
                        account_id, source_group_id, source_message_id, target_group_id,
                        status, target_message_id, comment_status, comment_message_id, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '', '', '', '', ?, ?)
                    ON CONFLICT (account_id, source_group_id, source_message_id, target_group_id)
                    DO UPDATE SET
                        status = excluded.status,
                        target_message_id = '',
                        comment_status = '',
                        comment_message_id = '',
                        reason = '',
                        updated_at = excluded.updated_at
                    WHERE {_TABLE}.status = ?
                    """,
                    (*_key_values(key), STATUS_SENDING, now, now, STATUS_FAILED),
                )
                granted = cursor.rowcount == 1
                record = self._fetch(cursor, key)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from error
        if record is None:
            raise LedgerUnavailableError("投递占位登记后无法读回记录")
        return Reservation(granted=granted, record=record)

    def recover_interrupted(self) -> int:
        """把上一次进程中断遗留的 `sending` 归并为 `unknown`，返回归并条数。

        只应在插件启动（刚打开账本）后调用一次：本进程此时没有任何在途投递，
        遗留的 `sending` 一定来自被中断的进程，结果无法确认，必须按待核实处理，
        不能降级成可安全重试的 `failed`。

        恢复失败抛 `LedgerUnavailableError`（调用方至少告警一次）：失败与「归并
        零条」必须可区分；失败时遗留 `sending` 仍会阻止重发，方向依旧安全。
        """

        try:
            with self._transaction() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {_TABLE}
                    SET status = ?, reason = ?, updated_at = ?
                    WHERE status = ?
                    """,
                    (
                        STATUS_UNKNOWN,
                        "上一次进程中断遗留的发送记录，结果待核实",
                        self._clock.now(),
                        STATUS_SENDING,
                    ),
                )
                return cursor.rowcount
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from error

    def mark_sent(self, key: DeliveryKey, target_message_id: str) -> bool:
        """把在途占位登记为成功（保存真实目标消息 ID）。

        返回 `True` 表示已更新到在途占位；返回 `False` 表示没有可更新的 `sending`
        记录（占位未命中）；存储失败抛 `LedgerUnavailableError`。
        """

        updated = self._clock.now()
        return self._write(
            f"""
            UPDATE {_TABLE}
            SET status = ?, target_message_id = ?, comment_status = '', comment_message_id = '',
                reason = '', updated_at = ?
            WHERE {_key_where()}
              AND status = ?
            """,
            (STATUS_SENT, str(target_message_id), updated, *_key_values(key), STATUS_SENDING),
        )

    def mark_failed(self, key: DeliveryKey, reason: str) -> bool:
        """登记「明确未发送」的失败（允许后续显式重试）；存储失败抛异常。"""

        return self._finalize_status(key, STATUS_FAILED, reason)

    def mark_unknown(self, key: DeliveryKey, reason: str) -> bool:
        """登记「结果待核实」（阻止重发）；存储失败抛异常。"""

        return self._finalize_status(key, STATUS_UNKNOWN, reason)

    def record_comment(self, key: DeliveryKey, *, status: LedgerStatus, message_id: str = "") -> bool:
        """在已成功投递的记录上保存评论结果。

        返回 `False` 表示没有 `sent` 记录可附加（本次未登记）；存储失败抛
        `LedgerUnavailableError`。
        """

        try:
            with self._transaction() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {_TABLE}
                    SET comment_status = ?, comment_message_id = ?, updated_at = ?
                    WHERE {_key_where()}
                      AND status = ?
                    """,
                    (str(status), str(message_id), self._clock.now(), *_key_values(key), STATUS_SENT),
                )
                return cursor.rowcount == 1
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from error

    def get(self, key: DeliveryKey) -> DeliveryRecord | None:
        """读取记录快照：不存在返回 `None`；读取失败抛 `LedgerUnavailableError`。"""

        try:
            with self._transaction() as cursor:
                return self._fetch(cursor, key)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from error

    def close(self) -> None:
        """关闭连接（不删除任何数据）。"""

        try:
            self._connection.close()
        except sqlite3.Error:
            return

    def _finalize_status(self, key: DeliveryKey, status: LedgerStatus, reason: str) -> bool:
        """把在途占位登记为终态（failed / unknown）。"""

        return self._write(
            f"""
            UPDATE {_TABLE}
            SET status = ?, reason = ?, updated_at = ?
            WHERE {_key_where()}
              AND status = ?
            """,
            (str(status), str(reason), self._clock.now(), *_key_values(key), STATUS_SENDING),
        )

    def _write(self, sql: str, parameters: tuple[object, ...]) -> bool:
        """执行一条更新：返回是否命中在途占位；存储失败抛 `LedgerUnavailableError`。"""

        try:
            with self._transaction() as cursor:
                cursor.execute(sql, parameters)
                return cursor.rowcount == 1
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from error

    def _fetch(self, cursor: sqlite3.Cursor, key: DeliveryKey) -> DeliveryRecord | None:
        """在给定游标（同一事务）内读回记录。"""

        cursor.execute(
            f"SELECT {', '.join(LEDGER_COLUMNS)} FROM {_TABLE} WHERE {_key_where()}",
            _key_values(key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return DeliveryRecord(key=key, **dict(zip(LEDGER_COLUMNS, row)))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """短事务：`BEGIN IMMEDIATE` → 提交/回滚；绝不跨越任何 `await`。

        BEGIN、事务体与 COMMIT 任一失败都显式回滚：连接若停在未结束事务，下一次
        `BEGIN` 会报 `cannot start a transaction within a transaction`，把一次
        存储故障扩散成整个账本不可用。游标创建与关闭统一放在 finally，任何分支
        都不泄漏游标。
        """

        cursor: sqlite3.Cursor | None = None
        try:
            try:
                cursor = self._connection.cursor()
                cursor.execute("BEGIN IMMEDIATE")
            except BaseException:
                self._rollback_quietly()
                raise
            try:
                yield cursor
            except BaseException:
                self._rollback_quietly()
                raise
            try:
                self._connection.commit()
            except BaseException:
                self._rollback_quietly()
                raise
        finally:
            if cursor is not None:
                cursor.close()

    def _rollback_quietly(self) -> None:
        """回滚当前事务；没有事务或回滚本身失败时都不掩盖原始异常。"""

        try:
            self._connection.rollback()
        except sqlite3.Error:
            return


def _key_values(key: DeliveryKey) -> tuple[str, str, str, str]:
    """键值按固定列顺序展开（参数绑定用）。"""

    return (key.account_id, key.source_group_id, key.source_message_id, key.target_group_id)


def _key_where() -> str:
    """键匹配条件（列名固定，值全部走参数绑定）。"""

    return (
        "account_id = ? AND source_group_id = ? AND source_message_id = ? AND target_group_id = ?"
    )
