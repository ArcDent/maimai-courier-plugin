"""服务端账本的显式初始化、只读状态与旧账本迁移/导出（design D10、Migration Plan）。

- 首次使用必须**显式**选择 `fresh`（新装）或 `migrate`（从本插件旧账本只读导入）：
  `initialize_fresh` 遇到既有身份或既有记录一律拒绝，绝不隐式覆盖成新装；
- 每次初始化/迁移都先取状态目录独占锁：服务进程活着时拒绝离线改写（导出要求
  「服务已停止发送」也由同一把锁保证）；
- 数据集元数据只保存账本身份、schema 版本、创建时间与导入统计（快照摘要 + 计数），
  **不含凭证**（凭证只在服务进程的环境变量里，design D5）；
- 旧账本只读访问：`legacy_record_count` / 迁移只读快照，绝不修改或删除原库。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from shared.clock import Clock, SystemClock
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    LedgerUnavailable,
)
from shared.protocol import COMMENT_STATUS_VALUES, delivery_id_for

from .ledger_store import (
    ATTEMPT_TABLE,
    COMMENT_NOT_REQUESTED,
    COMMENT_PENDING,
    COMMENT_SENDING,
    COMMENT_SKIPPED,
    COMMENT_UNKNOWN,
    DELIVERY_TABLE,
    LEGACY_DELIVERY_COLUMNS,
    LEGACY_LEDGER_FILE_NAME,
    META_CREATED_AT,
    META_IDENTITY,
    META_IMPORTS,
    META_SCHEMA_VERSION,
    META_TABLE,
    SCHEMA_VERSION,
    StateDatabase,
    describe_ledger_error,
    open_state_database,
    read_meta,
    state_transaction,
)


@dataclass(frozen=True, slots=True)
class FreshOutcome:
    """新装初始化结果：`created` 为假表示确认既有身份（幂等，未改动任何记录）。"""

    ledger_identity: str
    created: bool


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """一次已完成导入的摘要（写入元数据，供重复导入幂等判定与只读报告）。

    `statuses` 是新导入记录按**映射后**状态（`sent` / `unknown` / `failed`）的计数；
    `legacy_statuses` 是快照里的原状态分布（含 `sending`），供人工核对。
    """

    snapshot_sha256: str
    records: int
    statuses: Mapping[str, int] = field(default_factory=dict)
    legacy_statuses: Mapping[str, int] = field(default_factory=dict)
    unchanged: int = 0
    imported_at: float = 0.0

    def as_json(self) -> dict[str, Any]:
        """元数据里的 JSON 形态（键名固定，不含任何路径或凭证）。"""

        return {
            "snapshot_sha256": self.snapshot_sha256,
            "records": self.records,
            "statuses": dict(self.statuses),
            "legacy_statuses": dict(self.legacy_statuses),
            "unchanged": self.unchanged,
            "imported_at": self.imported_at,
        }

    @classmethod
    def from_json(cls, raw: object) -> "ImportSummary | None":
        """从元数据 JSON 还原；形态不符返回 `None`（不猜测字段）。"""

        if not isinstance(raw, Mapping):
            return None
        digest = raw.get("snapshot_sha256")
        records = raw.get("records")
        if not isinstance(digest, str) or not isinstance(records, int):
            return None
        unchanged = raw.get("unchanged")
        imported_at = raw.get("imported_at")
        return cls(
            snapshot_sha256=digest,
            records=int(records),
            statuses=_int_mapping(raw.get("statuses")),
            legacy_statuses=_int_mapping(raw.get("legacy_statuses")),
            unchanged=int(unchanged) if isinstance(unchanged, int) else 0,
            imported_at=float(imported_at) if isinstance(imported_at, (int, float)) else 0.0,
        )


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """旧账本迁移结果：账本身份、快照摘要与导入统计（不发送、不改原库）。"""

    ledger_identity: str
    snapshot_sha256: str
    records: int
    statuses: Mapping[str, int]
    legacy_statuses: Mapping[str, int]
    unchanged: int = 0
    already_imported: bool = False


@dataclass(frozen=True, slots=True)
class ExportOutcome:
    """兼容导出结果：目标文件、源账本身份、记录数与导出后的状态分布。"""

    output: str
    ledger_identity: str
    records: int
    statuses: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class LedgerStatusReport:
    """只读账本报告：身份、schema 版本、各状态计数与导入摘要（不含任何正文）。"""

    ledger_identity: str
    schema_version: str
    initialized: bool
    deliveries: Mapping[str, int]
    attempts: int
    imports: tuple[ImportSummary, ...]


def initialize_fresh(
    state_dir: Path,
    *,
    confirm_ledger_id: str = "",
    clock: Clock | None = None,
) -> FreshOutcome | LedgerUnavailable:
    """新装初始化：生成稳定账本身份并写入元数据（已有状态一律拒绝）。

    - 已有身份：只有 `--confirm-ledger-id` 与之完全一致才幂等成功（不覆盖）；
      其余情况一律拒绝，并提示用 `ledger migrate` 处理已有安装；
    - 无身份但有投递/尝试记录：状态来源不明，拒绝（不得当作空账本）；
    - 无身份且无记录：检测到本插件旧账本（`delivery_ledger.sqlite3`）且有记录、或旧库
      不可读时拒绝，要求显式 `migrate`；
    - 空目标上传入非空确认 ID 视为指向了另一个数据集，拒绝。
    """

    time_source = clock or SystemClock()
    database = open_state_database(Path(state_dir), writable=True, create=True)
    if isinstance(database, LedgerUnavailable):
        return database
    try:
        return _fresh_locked(database, state_dir=Path(state_dir), confirm_ledger_id=confirm_ledger_id, clock=time_source)
    finally:
        database.close()


def _fresh_locked(
    database: StateDatabase, *, state_dir: Path, confirm_ledger_id: str, clock: Clock
) -> FreshOutcome | LedgerUnavailable:
    """在已持锁的状态库上执行新装初始化（由 `initialize_fresh` 调用）。"""

    confirm = str(confirm_ledger_id or "").strip()
    connection = database.connection
    try:
        identity = read_meta(connection, META_IDENTITY).strip()
        deliveries = _count_rows(connection, DELIVERY_TABLE)
        attempts = _count_rows(connection, ATTEMPT_TABLE)
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))

    if identity:
        if confirm and confirm == identity:
            return FreshOutcome(ledger_identity=identity, created=False)
        if confirm:
            return LedgerUnavailable(
                "确认的账本身份与既有账本不符：拒绝把该数据集当作新装账本"
            )
        return LedgerUnavailable(
            f"该状态目录已初始化为账本身份 {identity}：已有状态不得隐式覆盖成 fresh；"
            "确认沿用请加 --confirm-ledger-id，从旧插件账本升级请用 `ledger migrate`"
        )
    if deliveries or attempts:
        return LedgerUnavailable(
            "账本已有投递记录但缺少账本身份（状态来源不明）：拒绝 fresh，请人工核对或用 `ledger migrate`"
        )
    if confirm:
        return LedgerUnavailable(
            "目标账本尚未初始化（不存在该账本身份）：确认 ID 指向了另一个数据集，拒绝 fresh"
        )

    legacy = state_dir / LEGACY_LEDGER_FILE_NAME
    if legacy.exists():
        records = legacy_record_count(legacy)
        if records is None:
            return LedgerUnavailable(
                "状态目录里存在无法读取的旧插件账本（状态来源不明）：拒绝 fresh，"
                "请核对后再用 `ledger migrate`"
            )
        if records > 0:
            return LedgerUnavailable(
                f"检测到本插件旧账本（{LEGACY_LEDGER_FILE_NAME}，{records} 条记录）："
                "已有安装只允许 `ledger migrate`，拒绝以 fresh 丢弃历史"
            )

    generated = secrets.token_hex(16)
    now = clock.now()
    try:
        with state_transaction(connection) as cursor:
            _write_meta(cursor, META_SCHEMA_VERSION, SCHEMA_VERSION)
            _write_meta(cursor, META_IDENTITY, generated)
            _write_meta(cursor, META_CREATED_AT, repr(float(now)))
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))
    return FreshOutcome(ledger_identity=generated, created=True)


def read_ledger_status(state_dir: Path) -> LedgerStatusReport | LedgerUnavailable:
    """只读报告账本身份与计数（不取锁、不建表、不修改文件；服务运行中也可读取）。"""

    database = open_state_database(Path(state_dir), writable=False)
    if isinstance(database, LedgerUnavailable):
        return database
    connection = database.connection
    try:
        identity = read_meta(connection, META_IDENTITY).strip()
        schema_version = read_meta(connection, META_SCHEMA_VERSION).strip()
        deliveries = _status_counts(connection)
        attempts = _count_rows(connection, ATTEMPT_TABLE)
        imports_raw = read_meta(connection, META_IMPORTS).strip()
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))
    finally:
        database.close()

    imports = parse_imports(imports_raw)
    if isinstance(imports, LedgerUnavailable):
        return imports

    return LedgerStatusReport(
        ledger_identity=identity,
        schema_version=schema_version,
        initialized=bool(identity),
        deliveries=deliveries,
        attempts=attempts,
        imports=imports,
    )


def parse_imports(raw: str) -> tuple[ImportSummary, ...] | LedgerUnavailable:
    """解析元数据里的导入摘要列表（幂等判定的唯一依据）。

    - 元数据为空（从未导入）返回空元组；
    - 非空但 JSON 不可解析、不是列表或含形态不符的条目时**不 fail-open**：
      返回 `LedgerUnavailable` 并要求人工核对——把损坏摘要当作「没有导入」会让
      重复导入绕过幂等快速路径，进而在无冲突时静默重写摘要、丢失原统计。
    """

    text = str(raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return LedgerUnavailable(
            "账本元数据 imports 不是有效 JSON（可能已损坏）：拒绝据此判定重复导入，请人工核对"
        )
    if not isinstance(data, list):
        return LedgerUnavailable(
            "账本元数据 imports 不是摘要列表（形态异常）：拒绝据此判定重复导入，请人工核对"
        )
    parsed: list[ImportSummary] = []
    for item in data:
        summary = ImportSummary.from_json(item)
        if summary is None:
            return LedgerUnavailable(
                "账本元数据 imports 含无法解析的摘要条目：拒绝据此判定重复导入，请人工核对"
            )
        parsed.append(summary)
    return tuple(parsed)


def legacy_snapshot_digest(path: Path) -> str:
    """快照字节指纹（SHA-256）：迁移前后必须一致（只读导入不改写原库）。"""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_record_count(path: Path) -> int | None:
    """只读读取旧插件账本的记录数；不可读（损坏/缺表/权限）返回 `None`。

    永远以 `mode=ro` 打开：本工具不修改、不删除旧库。
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
        row = connection.execute(f"SELECT COUNT(*) FROM {DELIVERY_TABLE}").fetchone()
    except (sqlite3.Error, OSError):
        return None
    finally:
        if connection is not None:
            connection.close()
    return int(row[0]) if row is not None else 0


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    """`COUNT(*)`（表名只来自本模块常量，用户数据一律参数绑定）。"""

    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0


@dataclass(frozen=True, slots=True)
class LegacyRecord:
    """旧账本的一条投递记录（只读读出，字段与旧 schema 一一对应）。"""

    key: DeliveryKey
    status: str
    target_message_id: str
    comment_status: str
    comment_message_id: str
    reason: str
    created_at: float
    updated_at: float


class _MigrationStop(Exception):
    """迁移必须停止（冲突等）：抛出以触发事务回滚，不留下半份导入。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def read_legacy_snapshot(path: Path) -> tuple[LegacyRecord, ...] | LedgerUnavailable:
    """只读读取旧账本快照：先校验 schema 与完整性，任何不符都返回不可用。

    - 打开方式固定 `mode=ro`：本工具永远不修改、不删除旧库；
    - `PRAGMA integrity_check` 必须为 `ok`（数据页损坏即拒绝，绝不当作空历史）；
    - 列集合必须与已核实的旧 schema 完全一致（未知 schema 拒绝）；
    - 来源状态必须是旧四态之一，未知取值拒绝（不猜测、不静默丢弃）。
    """

    snapshot = Path(path)
    if not snapshot.is_file():
        return LedgerUnavailable("旧账本快照不存在或不是文件：拒绝当作空历史开始迁移")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{snapshot.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            return LedgerUnavailable("旧账本快照未通过完整性检查（可能损坏）：拒绝导入")
        columns = [
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({DELIVERY_TABLE})")
        ]
        if set(columns) != set(LEGACY_DELIVERY_COLUMNS):
            return LedgerUnavailable("旧账本快照的列与已知旧 schema 不符（未知 schema）：拒绝导入")
        rows = connection.execute(
            f"SELECT {', '.join(LEGACY_DELIVERY_COLUMNS)} FROM {DELIVERY_TABLE}"
        ).fetchall()
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))
    finally:
        if connection is not None:
            connection.close()

    records: list[LegacyRecord] = []
    for row in rows:
        status = str(row[4])
        if status not in (STATUS_SENDING, STATUS_SENT, STATUS_FAILED, STATUS_UNKNOWN):
            return LedgerUnavailable(
                f"旧账本快照含未知状态 {status!r}：无法无损合并，停止导入"
            )
        records.append(
            LegacyRecord(
                key=DeliveryKey(
                    account_id=str(row[0]),
                    source_group_id=str(row[1]),
                    source_message_id=str(row[2]),
                    target_group_id=str(row[3]),
                ),
                status=status,
                target_message_id=str(row[5]),
                comment_status=str(row[6]),
                comment_message_id=str(row[7]),
                reason=str(row[8]),
                created_at=float(row[9]),
                updated_at=float(row[10]),
            )
        )
    return tuple(records)


def migrate_legacy_snapshot(
    state_dir: Path,
    snapshot: Path,
    *,
    confirm_ledger_id: str = "",
    clock: Clock | None = None,
) -> MigrationOutcome | LedgerUnavailable:
    """从本插件旧账本快照**只读**导入（Migration Plan 3）：不发送、不改原库。

    - 目标已有身份：必须用 `--confirm-ledger-id` 与之一致（否则拒绝，防止把记录导入
      另一个数据集）；目标为空则生成新身份并保存快照摘要与导入统计；
    - 同一快照重复导入是幂等的：不再写入任何记录（返回原统计）；
    - 与目标已有记录冲突（同键不同状态/回执）时停止整个导入并回滚：
      不降低安全状态、不覆盖、不留下半份导入。
    """

    time_source = clock or SystemClock()
    records = read_legacy_snapshot(snapshot)
    if isinstance(records, LedgerUnavailable):
        return records
    try:
        digest = legacy_snapshot_digest(snapshot)
    except OSError as error:
        return LedgerUnavailable(describe_ledger_error(error))

    database = open_state_database(Path(state_dir), writable=True, create=True)
    if isinstance(database, LedgerUnavailable):
        return database
    try:
        return _migrate_locked(
            database,
            records=records,
            digest=digest,
            confirm_ledger_id=confirm_ledger_id,
            clock=time_source,
        )
    finally:
        database.close()


def _migrate_locked(
    database: StateDatabase,
    *,
    records: tuple[LegacyRecord, ...],
    digest: str,
    confirm_ledger_id: str,
    clock: Clock,
) -> MigrationOutcome | LedgerUnavailable:
    """在已持锁的状态库上执行导入（由 `migrate_legacy_snapshot` 调用）。"""

    confirm = str(confirm_ledger_id or "").strip()
    connection = database.connection
    try:
        identity = read_meta(connection, META_IDENTITY).strip()
        deliveries = _count_rows(connection, DELIVERY_TABLE)
        attempts = _count_rows(connection, ATTEMPT_TABLE)
        imports_raw = read_meta(connection, META_IMPORTS)
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))

    imports = parse_imports(imports_raw)
    if isinstance(imports, LedgerUnavailable):
        return imports

    for item in imports:
        if item.snapshot_sha256 == digest:
            return MigrationOutcome(
                ledger_identity=identity,
                snapshot_sha256=digest,
                records=item.records,
                statuses=item.statuses,
                legacy_statuses=item.legacy_statuses,
                unchanged=item.unchanged,
                already_imported=True,
            )

    if identity:
        if not confirm:
            return LedgerUnavailable(
                "迁移目标已有账本身份：请用 --confirm-ledger-id 确认把快照导入该数据集"
            )
        if confirm != identity:
            return LedgerUnavailable(
                "迁移确认的账本身份与目标账本不符：拒绝导入（可能指向了另一个数据集）"
            )
    elif confirm:
        return LedgerUnavailable(
            "目标账本尚未初始化（不存在该账本身份）：确认 ID 指向另一个数据集，拒绝导入"
        )
    elif deliveries or attempts:
        return LedgerUnavailable(
            "账本已有投递记录但缺少账本身份（状态来源不明）：拒绝导入，请人工核对"
        )

    legacy_counts: dict[str, int] = {}
    for record in records:
        legacy_counts[record.status] = legacy_counts.get(record.status, 0) + 1

    new_identity = identity or secrets.token_hex(16)
    now = clock.now()
    statuses: dict[str, int] = {}
    imported = 0
    unchanged = 0
    try:
        with state_transaction(connection) as cursor:
            for record in records:
                mapped = _map_legacy(record)
                existing = _existing_delivery_row(cursor, record.key)
                if existing is not None:
                    if existing != mapped[:4]:
                        raise _MigrationStop(
                            "服务账本里已有同一投递键的不同记录，无法无损合并：停止导入"
                            "（不覆盖、不降低已有安全状态）"
                        )
                    unchanged += 1
                    continue
                cursor.execute(
                    f"""
                    INSERT INTO {DELIVERY_TABLE} (
                        account_id, source_group_id, source_message_id, target_group_id,
                        status, target_message_id, comment_status, comment_message_id,
                        reason, created_at, updated_at, delivery_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *(
                            record.key.account_id,
                            record.key.source_group_id,
                            record.key.source_message_id,
                            record.key.target_group_id,
                        ),
                        *mapped,
                        record.created_at,
                        record.updated_at,
                        delivery_id_for(record.key),
                    ),
                )
                imported += 1
                statuses[mapped[0]] = statuses.get(mapped[0], 0) + 1

            summary = ImportSummary(
                snapshot_sha256=digest,
                records=imported,
                statuses=statuses,
                legacy_statuses=legacy_counts,
                unchanged=unchanged,
                imported_at=now,
            )
            _write_meta(cursor, META_SCHEMA_VERSION, SCHEMA_VERSION)
            _write_meta(cursor, META_IDENTITY, new_identity)
            _write_meta(
                cursor,
                META_IMPORTS,
                json.dumps(
                    [item.as_json() for item in imports] + [summary.as_json()],
                    ensure_ascii=False,
                ),
            )
    except _MigrationStop as stop:
        return LedgerUnavailable(stop.reason)
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))

    return MigrationOutcome(
        ledger_identity=new_identity,
        snapshot_sha256=digest,
        records=imported,
        statuses=statuses,
        legacy_statuses=legacy_counts,
        unchanged=unchanged,
        already_imported=False,
    )


def export_compatible_snapshot(
    state_dir: Path,
    output: Path,
    *,
    confirm_ledger_id: str = "",
) -> ExportOutcome | LedgerUnavailable:
    """回滚导出（Migration Plan 7）：写出含**旧记录与新投递**的旧 schema 兼容快照。

    - 先取状态目录独占锁：服务仍在运行时（或另一个离线工具在进行）拒绝导出，
      从而保证「服务已停止发送」；
    - 保守映射：不确定的源状态（`sending`）导出为待核实、评论不自动补齐；
      `sent` 保留回执与评论、`unknown` 保留阻断、`failed` 保留为可重试；
    - 先写临时文件、**校验**（旧 schema + 键集合/数量与源账本一致）后再以
      `os.link` 原子落成目标：目标已存在（含「检查之后并发出现」）即失败，
      绝不覆盖任何既有文件；成功后清理临时文件；
    - 任一步失败都不改写服务账本，也不覆盖已有输出文件；目标路径由操作者显式指定。
    """

    confirm = str(confirm_ledger_id or "").strip()
    target = Path(output)
    if not target.parent.is_dir():
        return LedgerUnavailable("导出目标目录不存在：拒绝隐式创建目录，请显式给出已存在的目录")
    if target.exists():
        return LedgerUnavailable("导出目标已存在：拒绝覆盖，请改用新路径并人工切换")

    database = open_state_database(Path(state_dir), writable=True, create=False)
    if isinstance(database, LedgerUnavailable):
        return database
    connection = database.connection
    try:
        identity = read_meta(connection, META_IDENTITY).strip()
        if not identity:
            return LedgerUnavailable(
                "服务账本尚未初始化（缺少账本身份）：拒绝导出成「空历史」快照"
            )
        if confirm and confirm != identity:
            return LedgerUnavailable("导出确认的账本身份与源账本不符：拒绝导出（可能指向了另一个数据集）")
        source_rows = connection.execute(
            f"SELECT {', '.join(LEGACY_DELIVERY_COLUMNS)}, delivery_id FROM {DELIVERY_TABLE}"
        ).fetchall()
    except (sqlite3.Error, OSError) as error:
        return LedgerUnavailable(describe_ledger_error(error))
    finally:
        database.close()

    expected_keys = {
        (str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in source_rows
    }
    temp_path = target.with_name(f"{target.name}.tmp-{secrets.token_hex(4)}")
    try:
        statuses = _write_legacy_snapshot(temp_path, source_rows)
        verified = read_legacy_snapshot(temp_path)
        if isinstance(verified, LedgerUnavailable):
            return LedgerUnavailable(f"导出校验失败：{verified.reason}；目标文件未被替换")
        verified_keys = {
            (
                record.key.account_id,
                record.key.source_group_id,
                record.key.source_message_id,
                record.key.target_group_id,
            )
            for record in verified
        }
        if len(verified) != len(source_rows) or verified_keys != expected_keys:
            return LedgerUnavailable(
                "导出校验失败：键集合或记录数量与源账本不一致；目标文件未被替换"
            )
        try:
            # `os.link` 的「目标存在即失败」语义是原子的：并发出现的既有文件绝不会
            # 被覆盖；成功后临时文件即被清理，失败时也不残留。
            os.link(temp_path, target)
        except FileExistsError:
            return LedgerUnavailable(
                "导出目标已存在：拒绝覆盖（检查之后出现了并发写入者），请改用新路径并人工切换"
            )
        except OSError as error:
            return LedgerUnavailable(describe_ledger_error(error))
        finally:
            _remove_quietly(temp_path)
    except (sqlite3.Error, OSError) as error:
        _remove_quietly(temp_path)
        return LedgerUnavailable(describe_ledger_error(error))
    except BaseException:
        _remove_quietly(temp_path)
        raise
    return ExportOutcome(
        output=str(target),
        ledger_identity=identity,
        records=len(source_rows),
        statuses=statuses,
    )


def _write_legacy_snapshot(path: Path, rows: list[Any]) -> dict[str, int]:
    """把服务账本的投递行写成一个只含旧 schema 的新库（参数绑定，无用户数据拼接）。

    返回导出后的状态分布（供 CLI 报告与人工核对）。
    """

    statuses: dict[str, int] = {}
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        connection.execute(
            f"""
            CREATE TABLE {DELIVERY_TABLE} (
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
        connection.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                mapped = _export_row(row)
                connection.execute(
                    f"INSERT INTO {DELIVERY_TABLE} ({', '.join(LEGACY_DELIVERY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in LEGACY_DELIVERY_COLUMNS)})",
                    mapped[:11],
                )
                status = str(mapped[4])
                statuses[status] = statuses.get(status, 0) + 1
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()
    return statuses


def _export_row(row: Any) -> tuple[Any, ...]:
    """服务账本的一行 → 旧 schema 行（保守映射，评论不自动补齐）。

    列顺序与 `LEGACY_DELIVERY_COLUMNS` 一致；源状态 `sending`（服务被中断留下的在途
    占位）按待核实导出，绝不导出成可重试的失败。
    """

    status = str(row[4])
    if status == STATUS_SENDING:
        exported_status = STATUS_UNKNOWN
        reason = str(row[8]) or "服务中断遗留的在途占位，结果待核实（不会自动续发）"
    else:
        exported_status = status
        reason = str(row[8])
    return (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        exported_status,
        str(row[5]),
        _export_comment_status(str(row[6])),
        str(row[7]),
        reason,
        float(row[9]),
        float(row[10]),
    )


def _export_comment_status(value: str) -> str:
    """评论状态 → 旧 schema 值：空串沿用旧约定（未请求），在途按待核实、未提交按跳过。"""

    text = str(value or "").strip()
    if not text or text == COMMENT_NOT_REQUESTED:
        return ""
    if text == COMMENT_SENDING:
        return COMMENT_UNKNOWN
    if text == COMMENT_PENDING:
        return COMMENT_SKIPPED
    if text in COMMENT_STATUS_VALUES:
        return text
    return COMMENT_UNKNOWN


def _remove_quietly(path: Path) -> None:
    """删除临时导出文件；删除失败不掩盖原始结果。"""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _map_legacy(record: LegacyRecord) -> tuple[str, str, str, str, str]:
    """旧记录 → 服务端字段：返回（状态、目标 ID、评论状态、评论 ID、原因）。

    - `sent` 保留回执与评论；`unknown` 保留阻断；`failed` 保留为可显式重试；
    - `sending` 归并为待核实（可能已发出，绝不自动续发）。
    """

    comment_status = _map_legacy_comment(record.comment_status)
    if record.status == STATUS_SENT:
        return (
            STATUS_SENT,
            record.target_message_id,
            comment_status,
            record.comment_message_id,
            record.reason,
        )
    if record.status == STATUS_FAILED:
        return (
            STATUS_FAILED,
            "",
            comment_status,
            record.comment_message_id,
            record.reason,
        )
    if record.status == STATUS_SENDING:
        return (
            STATUS_UNKNOWN,
            "",
            comment_status,
            record.comment_message_id,
            "旧账本中未完成的发送记录，结果待核实（不会自动续发）",
        )
    return (
        STATUS_UNKNOWN,
        record.target_message_id,
        comment_status,
        record.comment_message_id,
        record.reason or "旧账本中的待核实记录",
    )


def _map_legacy_comment(value: str) -> str:
    """旧评论状态 → 服务端封闭值域：空串按「未请求」，未知取值按待核实。"""

    text = str(value or "").strip()
    if not text:
        return COMMENT_NOT_REQUESTED
    if text in COMMENT_STATUS_VALUES:
        return text
    return COMMENT_UNKNOWN


def _existing_delivery_row(
    cursor: sqlite3.Cursor, key: DeliveryKey
) -> tuple[str, str, str, str] | None:
    """目标账本里同键记录的（状态、目标 ID、评论状态、评论 ID）；不存在返回 `None`。"""

    cursor.execute(
        f"SELECT status, target_message_id, comment_status, comment_message_id "
        f"FROM {DELIVERY_TABLE} WHERE {_LEGACY_KEY_WHERE}",
        (
            key.account_id,
            key.source_group_id,
            key.source_message_id,
            key.target_group_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]), str(row[2]), str(row[3]))


_LEGACY_KEY_WHERE: str = (
    "account_id = ? AND source_group_id = ? AND source_message_id = ? AND target_group_id = ?"
)
"""四字段键匹配条件（列名固定，值全部参数绑定）。"""


def _status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """投递表各状态计数（只读；未知状态按原字面量给出，便于人工核对）。"""

    counts: dict[str, int] = {}
    for row in connection.execute(f"SELECT status, COUNT(*) FROM {DELIVERY_TABLE} GROUP BY status"):
        counts[str(row[0])] = int(row[1])
    return counts


def _write_meta(cursor: sqlite3.Cursor, key: str, value: str) -> None:
    """写入数据集元数据（键值都走参数绑定）。"""

    cursor.execute(
        f"INSERT INTO {META_TABLE} (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _int_mapping(raw: object) -> dict[str, int]:
    """JSON 里的计数映射 → `dict[str, int]`（形态不符的项忽略）。"""

    parsed: dict[str, int] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, int):
                parsed[str(key)] = int(value)
    return parsed
