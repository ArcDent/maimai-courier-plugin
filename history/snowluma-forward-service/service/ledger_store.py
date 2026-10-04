"""服务端独占账本：SQLite 仓储与短事务边界（design D7/D9，第 3 组）。

- **单实例独占**：状态目录是服务端自己的数据目录，服务进程用 `flock` 非阻塞独占锁
  打开；同一目录的第二个打开者一律得到 `LedgerUnavailable`（关闭发送），不共享
  SQLite 文件（design D7「网络客户端不共享访问该文件」）。
- **旧 schema 兼容**：投递表沿用旧插件账本的表名与全部旧列（第 1 组 1.6 核实），
  只额外带一个 `delivery_id` 摘要列，因此旧公共入口 `delivery_ledger.open_ledger`
  能直接读同一张表；尝试表与数据集元数据表是服务端新增。
- **短事务**：每次写入都是 `BEGIN IMMEDIATE` → 事务体 → `COMMIT` 的独立短事务，
  期间绝不 `await` 网络；BEGIN、事务体与 COMMIT 任一失败都显式回滚并抛
  `LedgerUnavailableError`，不把一次存储故障扩散成整个账本不可用。
- **不静默清理**：`sent` / `unknown` 记录不按 TTL 或容量删除，本模块不提供删除接口。
- **安全失败**：损坏、不可读或 schema 不符的库一律返回/抛出不可用，不覆盖、不删除、
  不当作空库；`None` 只表示「记录不存在」，`False` 只表示「没有可更新的在途占位」。

尝试与投递的完整状态机在 3.2–3.5 逐条实现（本模块是它们的唯一落点）。
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterator, cast

from shared.clock import Clock, SystemClock
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    LedgerStatus,
    LedgerUnavailable,
    LedgerUnavailableError,
    SendStatus,
)
from shared.ledger import (
    ATTEMPT_STAGE_VALUES,
    AttemptSnapshot,
    AttemptStage,
    CommentReservation,
    DeliverySnapshot,
    RecoveryReport,
    SourceReservation,
)
from shared.protocol import COMMENT_STATUS_VALUES, CommentStatus, delivery_id_for

SERVICE_LEDGER_FILE_NAME: Final[str] = "service_ledger.sqlite3"
"""服务端独占账本的文件名（状态目录由受控配置指定，不与插件 `data_dir` 共用）。"""

STATE_LOCK_FILE_NAME: Final[str] = "service.lock"
"""状态目录独占锁文件名（`flock` 目标；只是锁，不是状态数据）。"""

LEGACY_LEDGER_FILE_NAME: Final[str] = "delivery_ledger.sqlite3"
"""旧插件账本文件名（第 1 组 1.6 核实）：迁移快照的默认来源。"""

DELIVERY_TABLE: Final[str] = "delivery_ledger"
"""投递表名：与旧插件账本同名，旧公共入口可直接读同一张表（迁移/导出契约）。"""

ATTEMPT_TABLE: Final[str] = "delivery_attempts"
"""尝试表名：记录每次客户端请求的源/评论阶段状态与原执行结果（design D7）。"""

META_TABLE: Final[str] = "ledger_meta"
"""数据集元数据表名：账本身份、schema 版本、导入统计（不含凭证，design D5/D10）。"""

SCHEMA_VERSION: Final[str] = "1"
"""服务端账本 schema 版本（写入元数据；版本不符时按不可用拒绝）。"""

META_SCHEMA_VERSION: Final[str] = "schema_version"
"""元数据键：schema 版本。"""

META_IDENTITY: Final[str] = "ledger_identity"
"""元数据键：账本身份（初始化时生成、跨重启稳定；误连空库靠它识别）。"""

META_CREATED_AT: Final[str] = "created_at"
"""元数据键：身份创建时间（由注入时钟提供）。"""

META_IMPORTS: Final[str] = "imports"
"""元数据键：已完成导入的快照摘要与统计（JSON 列表；重复导入据此幂等）。"""

LEGACY_KEY_COLUMNS: Final[tuple[str, ...]] = (
    "account_id",
    "source_group_id",
    "source_message_id",
    "target_group_id",
)
"""投递四字段键（唯一防重身份；评论、请求 ID、进程都不参与）。"""

LEGACY_DELIVERY_COLUMNS: Final[tuple[str, ...]] = LEGACY_KEY_COLUMNS + (
    "status",
    "target_message_id",
    "comment_status",
    "comment_message_id",
    "reason",
    "created_at",
    "updated_at",
)
"""旧插件账本的全部列（1.6 核实的旧 DDL 列序）：导出/迁移共用同一清单。"""

SERVICE_DELIVERY_COLUMNS: Final[tuple[str, ...]] = LEGACY_DELIVERY_COLUMNS + ("delivery_id",)
"""服务端投递表列：旧列 + `delivery_id` 摘要列（按投递 ID 的只读查询用）。"""

ATTEMPT_COLUMNS: Final[tuple[str, ...]] = (
    "attempt_id",
    *LEGACY_KEY_COLUMNS,
    "request_id",
    "session_id",
    "policy_generation",
    "source_stage",
    "comment_stage",
    "comment",
    "target_message_id",
    "comment_message_id",
    "reason",
    "created_at",
    "updated_at",
)
"""尝试表列：定位（请求/会话/代次）+ 源/评论阶段 + 已知回执与原因。"""

ATTEMPT_PREPARED: Final[AttemptStage] = "prepared"
"""尝试的源阶段：已准备（未消费的准备，重启恢复即失效）。"""

ATTEMPT_SENDING: Final[AttemptStage] = "sending"
"""尝试的源阶段：已提交发送占位、尚无确认结果（待核实处置）。"""

ATTEMPT_CANCELLED: Final[AttemptStage] = "cancelled"
"""尝试的源阶段：未消费即失效（撤销或重启恢复），明确未发送。"""

COMMENT_NOT_REQUESTED: Final[CommentStatus] = "not_requested"
"""评论阶段状态：本次尝试没有评论（缺省/空/全空白）。"""

COMMENT_PENDING: Final[CommentStatus] = "pending"
"""评论阶段状态：已登记本次评论，等待源确认成功后由客户端提交授权。"""

COMMENT_SKIPPED: Final[CommentStatus] = "skipped"
"""评论阶段状态：评论未发送（源未成功、客户端未提交或重启后准备失效）。"""

COMMENT_SENDING: Final[CommentStatus] = "sending"
"""评论阶段状态：已取得评论发送资格（平台调用前的持久占位）。"""

COMMENT_SENT: Final[CommentStatus] = "sent"
"""评论阶段状态：已确认发送成功（带真实评论消息 ID）。"""

COMMENT_FAILED: Final[CommentStatus] = "failed"
"""评论阶段状态：明确未发送的失败。"""

COMMENT_UNKNOWN: Final[CommentStatus] = "unknown"
"""评论阶段状态：结果待核实（可能已发送，不得自动补发）。"""

_CREATE_DELIVERY_TABLE: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {DELIVERY_TABLE} (
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
    delivery_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)
)
"""

_CREATE_ATTEMPT_TABLE: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {ATTEMPT_TABLE} (
    attempt_id TEXT NOT NULL PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_group_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    target_group_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    source_stage TEXT NOT NULL,
    comment_stage TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    target_message_id TEXT NOT NULL DEFAULT '',
    comment_message_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CREATE_META_TABLE: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_DELIVERY_INDEX: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_{DELIVERY_TABLE}_delivery_id ON {DELIVERY_TABLE} (delivery_id)
"""
"""`delivery_id` 索引：账本永不清理，`lookup_delivery` 的等值查找不得退化为全表扫描。"""

_SELECT_DELIVERY: Final[str] = (
    "account_id, source_group_id, source_message_id, target_group_id, status, "
    "target_message_id, comment_status, comment_message_id, reason"
)
"""投递行读取列（显式列名，与 `_delivery_snapshot` 一一对应）。"""

_SELECT_ATTEMPT: Final[str] = ", ".join(ATTEMPT_COLUMNS)
"""尝试行读取列（显式列名，与 `_attempt_row` 一一对应）。"""


@dataclass(frozen=True, slots=True)
class StateLock:
    """状态目录独占锁句柄：持有到 `release()`（随账本关闭释放）。"""

    fd: int
    path: Path

    def release(self) -> None:
        """释放锁并关闭锁文件描述符；重复释放安全。"""

        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class StateDatabase:
    """打开成功后的状态库：连接 + 独占锁（由调用方负责 `close()`）。"""

    connection: sqlite3.Connection
    lock: StateLock | None = None

    def close(self) -> None:
        """关闭连接并释放独占锁；不删除任何投递事实。"""

        _close_quietly(self.connection)
        if self.lock is not None:
            self.lock.release()


@dataclass(frozen=True, slots=True)
class _AttemptRow:
    """尝试表的一行（内部读取用）：公开快照 + 所属投递键。"""

    attempt_id: str
    key: DeliveryKey
    request_id: str
    session_id: str
    policy_generation: int
    source_stage: AttemptStage
    comment_stage: CommentStatus
    comment: str
    target_message_id: str
    comment_message_id: str
    reason: str

    def snapshot(self) -> AttemptSnapshot:
        """转成公开快照（投递 ID 由四字段派生，不额外存储；同时带上四字段身份）。"""

        return AttemptSnapshot(
            attempt_id=self.attempt_id,
            delivery_id=delivery_id_for(self.key),
            request_id=self.request_id,
            session_id=self.session_id,
            policy_generation=self.policy_generation,
            source_stage=_as_attempt_stage(self.source_stage),
            comment_stage=_as_comment_status(self.comment_stage),
            comment=self.comment,
            target_message_id=self.target_message_id,
            comment_message_id=self.comment_message_id,
            reason=self.reason,
            key=self.key,
        )


def open_state_database(
    state_dir: Path, *, writable: bool, create: bool = False
) -> StateDatabase | LedgerUnavailable:
    """打开状态目录里的服务账本数据库。

    - `create=False`（服务启动与只读查看）：账本文件必须已由 `ledger fresh` 或
      `ledger migrate` 建立；缺失即不可用——服务不隐式创建一份没有历史的新账本；
    - `writable=True`：先取状态目录独占锁（单实例）；写入路径建表并校验 schema。
      只读路径用 `mode=ro` 连接，不取锁、不建表、不修改文件；
    - schema 与当前版本不符（含把旧插件账本改名为服务账本）即不可用：必须显式迁移，
      不得当作空库或兼容库继续使用。
    """

    directory = Path(state_dir)
    ledger_path = directory / SERVICE_LEDGER_FILE_NAME
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return LedgerUnavailable(describe_ledger_error(error))
    if not ledger_path.exists() and not create:
        return LedgerUnavailable(
            "服务账本尚未初始化：请先运行 `python -m service ledger fresh` 或 "
            "`ledger migrate`；拒绝隐式创建没有历史的新账本"
        )

    lock: StateLock | None = None
    if writable:
        acquired = _acquire_state_lock(directory)
        if isinstance(acquired, LedgerUnavailable):
            return acquired
        lock = acquired

    connection: sqlite3.Connection | None = None
    mismatch = ""
    try:
        if writable:
            connection = sqlite3.connect(str(ledger_path), timeout=5.0, isolation_level=None)
            if create:
                # 先只读识别既有投递表：存在但不符时拒绝，且**不执行任何 DDL**，
                # 避免在被误点的外部数据库里补建我们的表。
                mismatch = _delivery_table_mismatch(connection)
                if not mismatch:
                    _create_tables(connection)
                    mismatch = _schema_mismatch(connection)
            else:
                mismatch = _schema_mismatch(connection)
        else:
            connection = sqlite3.connect(
                f"{ledger_path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
                isolation_level=None,
            )
            mismatch = _schema_mismatch(connection)
    except (sqlite3.Error, OSError) as error:
        _close_quietly(connection)
        if lock is not None:
            lock.release()
        return LedgerUnavailable(describe_ledger_error(error))

    if mismatch:
        _close_quietly(connection)
        if lock is not None:
            lock.release()
        return LedgerUnavailable(mismatch)
    assert connection is not None
    return StateDatabase(connection=connection, lock=lock)


def open_service_ledger(
    state_dir: Path,
    *,
    expected_ledger_id: str | None = None,
    clock: Clock | None = None,
) -> ServiceLedger | LedgerUnavailable:
    """服务启动用的账本公开打开入口：独占打开并校验 schema，返回可用账本。

    - `expected_ledger_id=None`：不过绑定检查（只读管理/测试工具用；不用于发送路径）；
    - `expected_ledger_id` 为字符串：执行数据集绑定校验（缺预期 ID、账本未初始化或身份
      不符都拒绝），服务启动与会话建立必须走这一形态，失败即关闭发送；
    - 账本缺失、损坏、schema 不符或已被其它进程独占时同样返回 `LedgerUnavailable`。
    """

    database = open_state_database(state_dir, writable=True, create=False)
    if isinstance(database, LedgerUnavailable):
        return database
    if expected_ledger_id is not None:
        try:
            identity = read_meta(database.connection, META_IDENTITY)
        except (sqlite3.Error, OSError) as error:
            database.close()
            return LedgerUnavailable(describe_ledger_error(error))
        unavailable = ledger_binding_unavailable(expected_ledger_id, identity)
        if unavailable is not None:
            database.close()
            return unavailable
    return ServiceLedger(database.connection, clock or SystemClock(), lock=database.lock)


def _acquire_state_lock(directory: Path) -> StateLock | LedgerUnavailable:
    """取状态目录独占锁（非阻塞）：已被占用时给出可定位原因，不排队等待。"""

    lock_path = directory / STATE_LOCK_FILE_NAME
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        return LedgerUnavailable(describe_ledger_error(error))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        return LedgerUnavailable(
            "状态目录已被另一个服务进程独占（服务按单实例运行），拒绝打开账本并关闭发送"
        )
    return StateLock(fd=descriptor, path=lock_path)


def _create_tables(connection: sqlite3.Connection) -> None:
    """建表与索引：只做 `CREATE ... IF NOT EXISTS`，不覆盖也不删除既有文件。"""

    connection.execute(_CREATE_DELIVERY_TABLE)
    connection.execute(_CREATE_ATTEMPT_TABLE)
    connection.execute(_CREATE_META_TABLE)
    connection.execute(_CREATE_DELIVERY_INDEX)


def _table_columns(connection: sqlite3.Connection, table: str) -> list[tuple[str, int]]:
    """`PRAGMA table_info` → `(列名, 主键序位)` 列表（表名只来自本模块常量）。"""

    return [
        (str(row[1]), int(row[5])) for row in connection.execute(f"PRAGMA table_info({table})")
    ]


def _primary_columns(columns: list[tuple[str, int]]) -> list[str]:
    """主键列（按主键序位排序；非主键列为 0）。"""

    return [name for name, position in sorted(columns, key=lambda item: item[1]) if position]


def _schema_mismatch_message(table: str) -> str:
    """schema 不符的统一措辞（不执行 DDL、不覆盖数据）。"""

    return (
        f"服务账本表 {table} 的列与当前 schema 不符（可能是旧账本或未知版本）；"
        "请用 `python -m service ledger migrate` 显式迁移，不得当作空库使用"
    )


def _delivery_table_mismatch(connection: sqlite3.Connection) -> str:
    """投递表存在但列/主键不符时返回原因；表不存在或完全相符返回空串。

    只读检查，不执行任何 DDL：被误点到的外部数据库必须原样保留。
    """

    columns = _table_columns(connection, DELIVERY_TABLE)
    if not columns:
        return ""
    if {name for name, _ in columns} != set(SERVICE_DELIVERY_COLUMNS):
        return _schema_mismatch_message(DELIVERY_TABLE)
    if _primary_columns(columns) != list(LEGACY_KEY_COLUMNS):
        return "服务账本投递表的主键不是账号+源群+源消息+目标群四字段，拒绝使用"
    return ""


def _schema_mismatch(connection: sqlite3.Connection) -> str:
    """校验三张表的列集合、投递表主键与元数据里的 schema 版本（空串表示通过）。

    版本为空只出现在首次初始化（`create` 路径稍后写入）或未初始化账本：放过形状
    检查、由账本身份与数据集绑定校验拒绝；版本已写入但不等于 `SCHEMA_VERSION` 时
    返回可定位原因，与模块承诺「schema 版本不符即不可用」一致。
    """

    expected: tuple[tuple[str, frozenset[str]], ...] = (
        (DELIVERY_TABLE, frozenset(SERVICE_DELIVERY_COLUMNS)),
        (ATTEMPT_TABLE, frozenset(ATTEMPT_COLUMNS)),
        (META_TABLE, frozenset({"key", "value"})),
    )
    for table, columns in expected:
        info = _table_columns(connection, table)
        if not info:
            return f"服务账本缺少表 {table}，schema 不可识别，拒绝使用"
        if {name for name, _ in info} != set(columns):
            return _schema_mismatch_message(table)
    columns = _table_columns(connection, DELIVERY_TABLE)
    if _primary_columns(columns) != list(LEGACY_KEY_COLUMNS):
        return "服务账本投递表的主键不是账号+源群+源消息+目标群四字段，拒绝使用"
    version = read_meta(connection, META_SCHEMA_VERSION).strip()
    if version and version != SCHEMA_VERSION:
        return (
            f"服务账本 schema 版本为 {version}，当前版本为 {SCHEMA_VERSION}：拒绝使用"
            "（请按部署说明迁移或重建账本，不得当作空库或兼容库继续使用）"
        )
    return ""


@contextmanager
def state_transaction(
    connection: sqlite3.Connection, *, immediate: bool = True
) -> Iterator[sqlite3.Cursor]:
    """服务端账本的短事务：BEGIN → 事务体 → COMMIT；任一步失败都显式回滚。

    连接若停在未结束事务，下一次 BEGIN 会报 `cannot start a transaction within a
    transaction`，把一次存储故障扩散成整个账本不可用；因此 BEGIN、事务体与 COMMIT
    的失败都统一回滚。游标创建与关闭放在 finally，任何分支都不泄漏游标。
    服务账本（`ServiceLedger`）与初始化/迁移工具共用这一份纪律。
    """

    cursor: sqlite3.Cursor | None = None
    try:
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        except BaseException:
            _rollback_quietly(connection)
            raise
        try:
            yield cursor
        except BaseException:
            _rollback_quietly(connection)
            raise
        try:
            connection.commit()
        except BaseException:
            _rollback_quietly(connection)
            raise
    finally:
        if cursor is not None:
            cursor.close()


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    """回滚当前事务；没有事务或回滚本身失败都不掩盖原始异常。"""

    try:
        connection.rollback()
    except sqlite3.Error:
        return


def read_meta(connection: sqlite3.Connection, key: str) -> str:
    """读取数据集元数据值；键不存在返回空串（读取失败抛原生 `sqlite3.Error`）。"""

    cursor = connection.execute(f"SELECT value FROM {META_TABLE} WHERE key = ?", (key,))
    row = cursor.fetchone()
    return str(row[0]) if row is not None else ""


def ledger_binding_unavailable(
    expected_ledger_id: str, identity: str
) -> LedgerUnavailable | None:
    """数据集绑定校验：缺预期身份、账本未初始化或身份不符都返回不可用（禁止发送）。

    这是「误连空库 / 换了一个数据集」的唯一判定点：调用方（服务启动、会话建立）必须
    据此关闭发送，不得按地址自动信任新的数据集（design D10 Migrations Plan 4）。
    """

    expected = str(expected_ledger_id or "").strip()
    actual = str(identity or "").strip()
    if not expected:
        return LedgerUnavailable(
            "缺少预期账本身份配置（客户端迁移绑定检查未完成），拒绝开启发送"
        )
    if not actual:
        return LedgerUnavailable(
            "服务账本尚未初始化（缺少账本身份）：请先运行 `python -m service ledger fresh` "
            "或 `ledger migrate`"
        )
    if actual != expected:
        return LedgerUnavailable(
            "服务账本身份与配置的预期账本 ID 不符（可能误连了另一个数据集或空账本），拒绝开启发送"
        )
    return None


def describe_ledger_error(error: BaseException) -> str:
    """可定位、不含用户数据与路径的失败描述。"""

    if isinstance(error, sqlite3.DatabaseError) and "not a database" in str(error):
        return "服务账本文件损坏或不是 SQLite 数据库"
    if isinstance(error, sqlite3.OperationalError):
        return "服务账本不可读或状态目录不可写"
    if isinstance(error, OSError):
        return "状态目录不可用"
    return "服务账本访问失败"


def _close_quietly(connection: sqlite3.Connection | None) -> None:
    """关闭连接；关闭本身失败不掩盖原始结果。"""

    if connection is None:
        return
    try:
        connection.close()
    except sqlite3.Error:
        return


def _as_ledger_status(value: str) -> LedgerStatus:
    """库里的投递状态：契约外取值按待核实读取（阻断优先，绝不当作可重试失败）。"""

    if value in (STATUS_SENDING, STATUS_SENT, STATUS_FAILED, STATUS_UNKNOWN):
        return cast(LedgerStatus, value)
    return STATUS_UNKNOWN


def _as_attempt_stage(value: str) -> AttemptStage:
    """库里的尝试源阶段：契约外取值按待核实读取。"""

    if value in ATTEMPT_STAGE_VALUES:
        return cast(AttemptStage, value)
    return cast(AttemptStage, STATUS_UNKNOWN)


def _as_comment_status(value: str) -> CommentStatus:
    """库里的评论状态：契约外取值按「未请求」读取（不影响源投递事实）。"""

    if value in COMMENT_STATUS_VALUES:
        return cast(CommentStatus, value)
    return COMMENT_NOT_REQUESTED


def _comment_outcome(
    status: CommentStatus, comment_message_id: str, reason: str
) -> tuple[CommentStatus, str, str]:
    """评论终态归一：只有带真实评论消息 ID 的 `sent` 才能登记为成功。

    缺消息 ID 的「成功」、`sending` 与契约外状态一律收窄为 `unknown`（待核实），
    绝不自动补发评论（design D7/D8）。
    """

    message = str(comment_message_id or "").strip()
    if status == COMMENT_SENT:
        if message:
            return COMMENT_SENT, message, ""
        return (
            COMMENT_UNKNOWN,
            "",
            f"{reason or '评论发送成功但缺少可核实的真实目标消息 ID'}，按待核实登记",
        )
    if status == COMMENT_FAILED:
        return COMMENT_FAILED, "", str(reason or "")
    if status == COMMENT_UNKNOWN:
        return COMMENT_UNKNOWN, "", str(reason or "评论结果待核实")
    if status == COMMENT_SKIPPED:
        return COMMENT_SKIPPED, "", str(reason or "")
    return COMMENT_UNKNOWN, "", f"{reason or '契约外的评论状态'}，按待核实登记"


def _attempt_stage_status(stage: AttemptStage) -> LedgerStatus:
    """尝试的源阶段 → 投递状态面（重放且没有投递记录时用于合成快照）。"""

    if stage in (STATUS_SENDING, STATUS_SENT, STATUS_FAILED, STATUS_UNKNOWN):
        return cast(LedgerStatus, stage)
    return STATUS_FAILED


def _synthesized_snapshot(key: DeliveryKey, status: LedgerStatus, reason: str) -> DeliverySnapshot:
    """没有投递记录时按尝试原结果合成的快照：只用于如实汇报，不写库。"""

    return DeliverySnapshot(delivery_id=delivery_id_for(key), key=key, status=status, reason=reason)


class ServiceLedger:
    """服务端账本本体：实现 `shared.ledger.LedgerRepository` 的 SQLite 仓储。

    返回 `None` 只表示「记录不存在」，返回 `False` 只表示「没有可更新的在途占位」；
    读写与恢复的存储故障一律抛 `LedgerUnavailableError`。
    """

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, *, lock: StateLock | None = None
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._lock = lock

    # ------------------------------------------------------------------ 尝试

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
        """登记一次「已准备」尝试：只记录定位与本次评论，不产生任何发送事实。

        - 同一尝试 ID 重复准备是幂等的：返回原快照，不改写定位与评论；
        - 尝试 ID 已被另一个投递占用时返回 `None`（拒绝），调用方不得据此发送；
        - 本方法不检查投递是否已阻断：占位时 `reserve_source` 统一拒绝并作废未消费
          的准备（准备成功不承诺可发送）。
        """

        now = self._clock.now()
        comment_text = str(comment or "")
        comment_stage: CommentStatus = (
            COMMENT_PENDING if comment_text.strip() else COMMENT_NOT_REQUESTED
        )
        try:
            with self._transaction() as cursor:
                existing = self._fetch_attempt(cursor, attempt_id)
                if existing is not None:
                    return existing.snapshot() if existing.key == key else None
                cursor.execute(
                    f"""
                    INSERT INTO {ATTEMPT_TABLE} (
                        attempt_id, account_id, source_group_id, source_message_id, target_group_id,
                        request_id, session_id, policy_generation, source_stage, comment_stage,
                        comment, target_message_id, comment_message_id, reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?)
                    """,
                    (
                        attempt_id,
                        *_key_values(key),
                        request_id,
                        session_id,
                        policy_generation,
                        ATTEMPT_PREPARED,
                        comment_stage,
                        comment_text,
                        now,
                        now,
                    ),
                )
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return AttemptSnapshot(
            attempt_id=attempt_id,
            delivery_id=delivery_id_for(key),
            request_id=request_id,
            session_id=session_id,
            policy_generation=policy_generation,
            source_stage=ATTEMPT_PREPARED,
            comment_stage=comment_stage,
            comment=comment_text,
            key=key,
        )

    async def lookup_attempt(self, attempt_id: str) -> AttemptSnapshot | None:
        """按尝试 ID 读取原执行结果；不存在返回 `None`，读取失败抛不可用。"""

        try:
            with self._transaction(immediate=False) as cursor:
                attempt = self._fetch_attempt(cursor, attempt_id)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return attempt.snapshot() if attempt is not None else None

    # ------------------------------------------------------------------ 投递

    async def reserve_source(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        request_id: str,
        session_id: str,
        policy_generation: int,
    ) -> SourceReservation:
        """登记源阶段占位（`sending`）并建立本次尝试。

        原子决定本次调用是否获得源发送资格：

        - 尝试 ID 已存在且不是「已准备」→ 旧尝试重放：只返回原结果，绝不再次发送；
        - 尝试 ID 属于另一个投递 → 拒绝，且不在本次投递留下任何记录；
        - 已有 `sent` / `sending` / `unknown` 投递记录 → 不授予；若本次带着未消费的
          准备，则该准备作废（明确未发送）；
        - 没有记录或既有记录是 `failed`（明确未发送）→ 登记占位并授予。

        占位在返回前提交，因此调用方只有在独立连接也可见之后才可能进入发送接缝。
        """

        now = self._clock.now()
        delivery_id = delivery_id_for(key)
        granted_comment: CommentStatus = COMMENT_NOT_REQUESTED
        try:
            with self._transaction() as cursor:
                attempt = self._fetch_attempt(cursor, attempt_id)
                existing = self._fetch_delivery(cursor, key)

                if attempt is not None and attempt.key != key:
                    return SourceReservation(
                        granted=False,
                        snapshot=existing
                        or _synthesized_snapshot(
                            key, STATUS_FAILED, "该尝试 ID 属于另一个投递，本次未产生发送"
                        ),
                    )
                if attempt is not None and attempt.source_stage != ATTEMPT_PREPARED:
                    return SourceReservation(
                        granted=False,
                        snapshot=existing
                        or _synthesized_snapshot(
                            key,
                            _attempt_stage_status(attempt.source_stage),
                            attempt.reason or "该尝试已结束，重复提交只返回原结果",
                        ),
                    )
                if existing is not None and existing.status != STATUS_FAILED:
                    if attempt is not None:
                        self._cancel_prepared_attempt(
                            cursor, attempt, now, "投递已有阻断记录，未消费的准备作废"
                        )
                    return SourceReservation(granted=False, snapshot=existing)

                granted_comment = (
                    attempt.comment_stage if attempt is not None else COMMENT_NOT_REQUESTED
                )
                if existing is None:
                    cursor.execute(
                        f"""
                        INSERT INTO {DELIVERY_TABLE} (
                            account_id, source_group_id, source_message_id, target_group_id,
                            status, target_message_id, comment_status, comment_message_id,
                            reason, created_at, updated_at, delivery_id
                        ) VALUES (?, ?, ?, ?, ?, '', ?, '', '', ?, ?, ?)
                        """,
                        (
                            *_key_values(key),
                            STATUS_SENDING,
                            granted_comment,
                            now,
                            now,
                            delivery_id,
                        ),
                    )
                else:
                    cursor.execute(
                        f"""
                        UPDATE {DELIVERY_TABLE}
                        SET status = ?, target_message_id = '', comment_status = ?,
                            comment_message_id = '', reason = '', updated_at = ?, delivery_id = ?
                        WHERE {_key_where()}
                        """,
                        (
                            STATUS_SENDING,
                            granted_comment,
                            now,
                            delivery_id,
                            *_key_values(key),
                        ),
                    )

                if attempt is None:
                    cursor.execute(
                        f"""
                        INSERT INTO {ATTEMPT_TABLE} (
                            attempt_id, account_id, source_group_id, source_message_id, target_group_id,
                            request_id, session_id, policy_generation, source_stage, comment_stage,
                            comment, target_message_id, comment_message_id, reason, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?)
                        """,
                        (
                            attempt_id,
                            *_key_values(key),
                            request_id,
                            session_id,
                            policy_generation,
                            STATUS_SENDING,
                            granted_comment,
                            now,
                            now,
                        ),
                    )
                else:
                    cursor.execute(
                        f"""
                        UPDATE {ATTEMPT_TABLE}
                        SET source_stage = ?, updated_at = ?
                        WHERE attempt_id = ?
                        """,
                        (STATUS_SENDING, now, attempt_id),
                    )
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return SourceReservation(
            granted=True,
            snapshot=DeliverySnapshot(
                delivery_id=delivery_id,
                key=key,
                status=STATUS_SENDING,
                comment_status=granted_comment,
            ),
        )

    async def finalize_source(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        status: SendStatus,
        target_message_id: str = "",
        reason: str = "",
    ) -> bool:
        """落源阶段终态：成功必须带真实目标消息 ID；返回是否命中在途占位。

        只命中「该尝试属于该投递键且源阶段为 `sending`、投递也在途」的占位；否则返回
        `False` 且不改写任何记录（已完成/待核实记录不接受迟到的终态登记）。
        缺真实目标 ID、`sending` 或契约外状态一律收窄为 `unknown`（待核实），
        绝不降级为可重试的 `failed`；源未确认成功时评论阶段一并记为 `skipped`。
        """

        final_status, final_target, final_reason = _source_outcome(
            status, target_message_id, reason
        )
        now = self._clock.now()
        try:
            with self._transaction() as cursor:
                attempt = self._fetch_attempt(cursor, attempt_id)
                if attempt is None or attempt.source_stage != STATUS_SENDING or attempt.key != key:
                    return False
                existing = self._fetch_delivery(cursor, key)
                if existing is None or existing.status != STATUS_SENDING:
                    return False

                comment_status = existing.comment_status
                comment_stage = attempt.comment_stage
                if final_status != STATUS_SENT:
                    if comment_status == COMMENT_PENDING:
                        comment_status = COMMENT_SKIPPED
                    if comment_stage == COMMENT_PENDING:
                        comment_stage = COMMENT_SKIPPED
                cursor.execute(
                    f"""
                    UPDATE {DELIVERY_TABLE}
                    SET status = ?, target_message_id = ?, comment_status = ?, reason = ?,
                        updated_at = ?
                    WHERE {_key_where()}
                    """,
                    (
                        final_status,
                        final_target,
                        comment_status,
                        final_reason,
                        now,
                        *_key_values(key),
                    ),
                )
                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET source_stage = ?, target_message_id = ?, comment = ?, comment_stage = ?,
                        reason = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        final_status,
                        final_target,
                        # 源未确认成功时评论不可能再提交：评论正文一并清空（最小数据）。
                        attempt.comment if final_status == STATUS_SENT else "",
                        comment_stage,
                        final_reason,
                        now,
                        attempt_id,
                    ),
                )
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return True

    async def reserve_comment(self, key: DeliveryKey, *, attempt_id: str) -> CommentReservation:
        """在源已确认成功且未落账之前不授予；同一尝试至多一次评论资格。

        只有「源阶段已 `sent` 且评论阶段仍 `pending`」的该投递尝试能取得评论发送资格；
        占位落盘（`sending`）后才返回，调用方此后才允许进入评论发送接缝。
        """

        now = self._clock.now()
        delivery_id = delivery_id_for(key)
        existing: DeliverySnapshot | None = None
        try:
            with self._transaction() as cursor:
                attempt = self._fetch_attempt(cursor, attempt_id)
                existing = self._fetch_delivery(cursor, key)
                if (
                    attempt is None
                    or attempt.key != key
                    or attempt.source_stage != STATUS_SENT
                    or attempt.comment_stage != COMMENT_PENDING
                    or existing is None
                    or existing.status != STATUS_SENT
                    or existing.comment_status != COMMENT_PENDING
                ):
                    return CommentReservation(
                        granted=False,
                        snapshot=existing
                        or _synthesized_snapshot(
                            key, STATUS_FAILED, "尚无成功的源投递，不能占位评论"
                        ),
                    )
                cursor.execute(
                    f"""
                    UPDATE {DELIVERY_TABLE}
                    SET comment_status = ?, updated_at = ?
                    WHERE {_key_where()}
                    """,
                    (COMMENT_SENDING, now, *_key_values(key)),
                )
                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET comment_stage = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (COMMENT_SENDING, now, attempt_id),
                )
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        assert existing is not None
        return CommentReservation(
            granted=True,
            snapshot=DeliverySnapshot(
                delivery_id=delivery_id,
                key=key,
                status=STATUS_SENT,
                target_message_id=existing.target_message_id,
                comment_status=COMMENT_SENDING,
            ),
        )

    async def finalize_comment(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        status: CommentStatus,
        comment_message_id: str = "",
        reason: str = "",
    ) -> bool:
        """保存评论结果（不覆盖源目标 ID）；返回是否命中对应阶段的尝试状态。

        - 代表「已尝试发送」的终态（`sent` / `failed` / `unknown`）必须命中评论占位
          （`sending`）：评论同样有发送前占位，不得凭空登记结果；
        - `skipped` 只用于「评论从未提交发送」：允许从 `pending` 登记（客户端未提交
          挑战或挑战到期），已占位（可能已发出）的评论不在此列；
        - 缺真实评论消息 ID 的「成功」与契约外状态一律收窄为 `unknown`（待核实），
          绝不降级为可重试失败；源目标 ID 与源状态不被触碰。
        """

        final_status, final_message, final_reason = _comment_outcome(
            status, comment_message_id, reason
        )
        expected_stage: CommentStatus = (
            COMMENT_PENDING if final_status == COMMENT_SKIPPED else COMMENT_SENDING
        )
        now = self._clock.now()
        try:
            with self._transaction() as cursor:
                attempt = self._fetch_attempt(cursor, attempt_id)
                if (
                    attempt is None
                    or attempt.key != key
                    or attempt.comment_stage != expected_stage
                ):
                    return False
                existing = self._fetch_delivery(cursor, key)
                if existing is None or existing.status != STATUS_SENT:
                    return False
                cursor.execute(
                    f"""
                    UPDATE {DELIVERY_TABLE}
                    SET comment_status = ?, comment_message_id = ?, reason = ?, updated_at = ?
                    WHERE {_key_where()}
                    """,
                    (
                        final_status,
                        final_message,
                        final_reason,
                        now,
                        *_key_values(key),
                    ),
                )
                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET comment_stage = ?, comment_message_id = ?, comment = '', reason = ?,
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (final_status, final_message, final_reason, now, attempt_id),
                )
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return True

    async def cancel_attempt(self, key: DeliveryKey, *, attempt_id: str, reason: str) -> bool:
        """撤销一次未消费的准备：源阶段转 `cancelled`（明确未发送）、评论转 `skipped`。

        只命中「该尝试属于该键且源阶段仍为 `prepared`」的未消费准备；已开始的阶段返回
        `False`（它们的真实结果由 `finalize_source` / `finalize_comment` 登记，不能被撤销
        改写），存储失败抛 `LedgerUnavailableError`（不伪称已撤销）。
        """

        now = self._clock.now()
        try:
            with self._transaction() as cursor:
                attempt = self._fetch_attempt(cursor, attempt_id)
                if attempt is None or attempt.key != key or attempt.source_stage != ATTEMPT_PREPARED:
                    return False
                self._cancel_prepared_attempt(cursor, attempt, now, reason)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return True

    async def lookup_delivery(self, delivery_id: str) -> DeliverySnapshot | None:
        """按投递 ID 读取权威状态；不存在返回 `None`，读取失败抛不可用。"""

        try:
            with self._transaction(immediate=False) as cursor:
                cursor.execute(
                    f"SELECT {_SELECT_DELIVERY} FROM {DELIVERY_TABLE} WHERE delivery_id = ?",
                    (delivery_id,),
                )
                row = cursor.fetchone()
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        if row is None:
            return None
        return _delivery_snapshot(row)

    async def recover_interrupted(self) -> RecoveryReport:
        """启动恢复：源/评论在途占位归并为待核实、未消费准备失效。

        - 源 `sending` → `unknown`（可能已发出，绝不自动续发）；
        - 评论 `sending` → `unknown`；
        - 未提交的评论准备（`pending`）→ `skipped`；
        - 未消费的准备（源 `prepared`）→ `cancelled`（旧会话/挑战随之失效；
          客户端可以重新准备出新的显式尝试）；
        - `sent` / `unknown` / `failed` 记录一律不动（不按 TTL 或容量清理）。

        `prepares_expired` 统计被失效的准备数（源 `prepared` + 评论 `pending`）。
        恢复失败抛 `LedgerUnavailableError`：调用方关闭发送并告警，遗留占位继续阻断。
        """

        now = self._clock.now()
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET source_stage = ?,
                        comment_stage = CASE WHEN comment_stage = ? THEN ? ELSE comment_stage END,
                        comment = CASE WHEN comment_stage = ? THEN '' ELSE comment END,
                        reason = ?, updated_at = ?
                    WHERE source_stage = ?
                    """,
                    (
                        ATTEMPT_CANCELLED,
                        COMMENT_PENDING,
                        COMMENT_SKIPPED,
                        COMMENT_PENDING,
                        "未消费的准备在服务重启后失效（未产生发送；可以重新准备）",
                        now,
                        ATTEMPT_PREPARED,
                    ),
                )
                prepares = cursor.rowcount

                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET comment_stage = ?, comment = '', reason = ?, updated_at = ?
                    WHERE comment_stage = ?
                    """,
                    (
                        COMMENT_SKIPPED,
                        "未提交的评论准备在服务重启后失效（未发送）",
                        now,
                        COMMENT_PENDING,
                    ),
                )
                prepares += cursor.rowcount

                cursor.execute(
                    f"""
                    UPDATE {DELIVERY_TABLE}
                    SET status = ?, reason = ?, updated_at = ?
                    WHERE status = ?
                    """,
                    (
                        STATUS_UNKNOWN,
                        "服务重启恢复：在途源发送占位归并为待核实（不会自动续发）",
                        now,
                        STATUS_SENDING,
                    ),
                )
                sources_recovered = cursor.rowcount

                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET source_stage = ?, reason = ?, updated_at = ?
                    WHERE source_stage = ?
                    """,
                    (
                        STATUS_UNKNOWN,
                        "服务重启恢复：在途源发送归并为待核实（不会自动续发）",
                        now,
                        STATUS_SENDING,
                    ),
                )

                cursor.execute(
                    f"""
                    UPDATE {ATTEMPT_TABLE}
                    SET comment_stage = ?, comment = '', reason = ?, updated_at = ?
                    WHERE comment_stage = ?
                    """,
                    (
                        STATUS_UNKNOWN,
                        "服务重启恢复：在途评论发送归并为待核实（不会自动补发）",
                        now,
                        COMMENT_SENDING,
                    ),
                )
                comments_recovered = cursor.rowcount

                cursor.execute(
                    f"""
                    UPDATE {DELIVERY_TABLE}
                    SET comment_status = ?, updated_at = ?
                    WHERE comment_status = ?
                    """,
                    (STATUS_UNKNOWN, now, COMMENT_SENDING),
                )
                cursor.execute(
                    f"""
                    UPDATE {DELIVERY_TABLE}
                    SET comment_status = ?, updated_at = ?
                    WHERE comment_status = ?
                    """,
                    (COMMENT_SKIPPED, now, COMMENT_PENDING),
                )
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return RecoveryReport(
            sources_recovered=sources_recovered,
            comments_recovered=comments_recovered,
            prepares_expired=prepares,
        )

    # ------------------------------------------------------------------ 数据集

    async def identity(self) -> str:
        """账本身份：初始化时生成、跨重启稳定；未初始化返回空串。

        误连空账本或另一个数据集靠它识别：调用方必须与受控配置里的预期账本 ID 校验
        （`ledger_binding_unavailable`），不匹配即关闭发送。
        """

        try:
            with self._transaction(immediate=False) as cursor:
                cursor.execute(
                    f"SELECT value FROM {META_TABLE} WHERE key = ?", (META_IDENTITY,)
                )
                row = cursor.fetchone()
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(describe_ledger_error(error)) from error
        return str(row[0]) if row is not None else ""

    # ------------------------------------------------------------------ 生命周期

    async def close(self) -> None:
        """关闭连接并释放独占锁；不删除任何投递事实。"""

        _close_quietly(self._connection)
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    # ------------------------------------------------------------------ 读取

    def _fetch_delivery(
        self, cursor: sqlite3.Cursor, key: DeliveryKey
    ) -> DeliverySnapshot | None:
        """在同一事务内按四字段键读回投递快照；不存在返回 `None`。"""

        cursor.execute(
            f"SELECT {_SELECT_DELIVERY} FROM {DELIVERY_TABLE} WHERE {_key_where()}",
            _key_values(key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _delivery_snapshot(row)

    def _fetch_attempt(self, cursor: sqlite3.Cursor, attempt_id: str) -> _AttemptRow | None:
        """在同一事务内按尝试 ID 读回尝试行；不存在返回 `None`。"""

        cursor.execute(
            f"SELECT {_SELECT_ATTEMPT} FROM {ATTEMPT_TABLE} WHERE attempt_id = ?",
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _attempt_row(row)

    def _cancel_prepared_attempt(
        self, cursor: sqlite3.Cursor, attempt: "_AttemptRow", now: float, reason: str
    ) -> None:
        """作废未消费的准备：源阶段转 `cancelled`（明确未发送），评论转 `skipped`。

        未消费的准备从未产生平台发送，评论正文不再需要：一并清空（最小数据）。
        """

        comment_stage: CommentStatus = (
            COMMENT_SKIPPED if attempt.comment_stage == COMMENT_PENDING else attempt.comment_stage
        )
        cursor.execute(
            f"""
            UPDATE {ATTEMPT_TABLE}
            SET source_stage = ?, comment_stage = ?, comment = '', reason = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (ATTEMPT_CANCELLED, comment_stage, reason, now, attempt.attempt_id),
        )

    # ------------------------------------------------------------------ 事务

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Cursor]:
        """短事务（与初始化/迁移工具共用 `state_transaction` 的同一纪律）。"""

        with state_transaction(self._connection, immediate=immediate) as cursor:
            yield cursor


def _key_values(key: DeliveryKey) -> tuple[str, str, str, str]:
    """键值按固定列顺序展开（参数绑定用）。"""

    return (key.account_id, key.source_group_id, key.source_message_id, key.target_group_id)


def _key_where() -> str:
    """键匹配条件（列名固定，值全部走参数绑定）。"""

    return (
        "account_id = ? AND source_group_id = ? AND source_message_id = ? AND target_group_id = ?"
    )


def _source_outcome(status: SendStatus, target_message_id: str, reason: str) -> tuple[str, str, str]:
    """源终态归一：只有带真实目标消息 ID 的 `sent` 才能登记为成功。

    缺真实目标 ID 的「成功」、`sending` 与契约外状态一律收窄为 `unknown`（待核实），
    绝不降级为可重试的 `failed`（design D7/D8）。
    """

    target = str(target_message_id or "").strip()
    if status == STATUS_SENT:
        if target:
            return STATUS_SENT, target, ""
        return (
            STATUS_UNKNOWN,
            "",
            f"{reason or '发送成功但缺少可核实的真实目标消息 ID'}，按待核实登记",
        )
    if status == STATUS_FAILED:
        return STATUS_FAILED, "", str(reason or "")
    if status == STATUS_UNKNOWN:
        return STATUS_UNKNOWN, "", str(reason or "发送结果待核实")
    if status == STATUS_SENDING:
        return STATUS_UNKNOWN, "", f"{reason or '发送仍在途、未取得终态'}，按待核实登记"
    return STATUS_UNKNOWN, "", f"{reason or '契约外的发送状态'}，按待核实登记"


def _delivery_snapshot(row: tuple[Any, ...]) -> DeliverySnapshot:
    """投递行 → 公开快照。"""

    key = DeliveryKey(
        account_id=str(row[0]),
        source_group_id=str(row[1]),
        source_message_id=str(row[2]),
        target_group_id=str(row[3]),
    )
    return DeliverySnapshot(
        delivery_id=delivery_id_for(key),
        key=key,
        status=_as_ledger_status(str(row[4])),
        target_message_id=str(row[5]),
        comment_status=_as_comment_status(str(row[6])),
        comment_message_id=str(row[7]),
        reason=str(row[8]),
    )


def _attempt_row(row: tuple[Any, ...]) -> _AttemptRow:
    """尝试行 → 内部读取模型（公开快照另经 `snapshot()`）。"""

    return _AttemptRow(
        attempt_id=str(row[0]),
        key=DeliveryKey(
            account_id=str(row[1]),
            source_group_id=str(row[2]),
            source_message_id=str(row[3]),
            target_group_id=str(row[4]),
        ),
        request_id=str(row[5]),
        session_id=str(row[6]),
        policy_generation=int(row[7]),
        source_stage=_as_attempt_stage(str(row[8])),
        comment_stage=_as_comment_status(str(row[9])),
        comment=str(row[10]),
        target_message_id=str(row[11]),
        comment_message_id=str(row[12]),
        reason=str(row[13]),
    )
