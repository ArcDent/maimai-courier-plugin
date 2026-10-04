"""投递账本：宿主 `data_dir` 内的 SQLite 最小持久状态（design D6）。

- **唯一键**：机器人账号 + 源群 + 源消息 ID + 目标群；评论不参与键，评论结果作为
  字段附属于源发送记录。
- **状态**：`sending`（已占位、尚未确认）/ `sent`（已确认投递）/ `failed`（明确未
  发送，允许后续显式重试）/ `unknown`（结果待核实，阻止重发）。
- **SQL 全部参数绑定**；每次操作都是独立短事务（`BEGIN IMMEDIATE` + `COMMIT`），
  绝不在网络等待期间持有事务；BEGIN、事务体与 COMMIT 任一失败都尝试有界回滚，
  无法确认事务结束则停用连接并保留独占，绝不继续读写污染连接。
- **不静默清理**：成功记录不按 TTL 或容量清除，本模块不提供任何删除接口。
- **安全失败**：库文件损坏或路径不可用时返回 `LedgerUnavailable`，调用方必须关闭
  发送；本模块不覆盖、不删除、也不把损坏库当作空库。
- **失败可区分**：读失败、写失败与恢复失败一律抛 `LedgerUnavailableError`；
  `None` 只表示「记录不存在」，`False` 只表示「没有可更新的在途占位」，两者不得
  与存储故障混同。
- **单实例独占**：打开数据库前取得本插件目录的非阻塞文件锁，关闭连接后释放；
  第二实例不打开数据库，更不会恢复或修改活实例的在途状态。

账本只管持久状态，不做发送、不做权限判断：编排见 `forward_delivery.py`。
"""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import stat
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable, Iterator, Union

from .clock import Clock, SystemClock
from .shared.delivery import (
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

# 投递身份、账本状态与账本错误类型的**唯一定义**在共享层 `shared/delivery.py`
# （服务进程同样需要且不能依赖插件侧模块，design D9）；本模块只做兼容重导出，
# 上面的导入保证 `delivery_ledger.DeliveryKey` 等名字与共享层是同一对象。

LEDGER_FILE_NAME = "delivery_ledger.sqlite3"
"""账本文件名（固定落在宿主授予的 `ctx.paths.data_dir` 下）。"""

STATE_LOCK_FILE_NAME = "delivery_ledger.lock"
"""直连账本新增独占锁；保留空锁文件，不把它当作服务切换收据。"""

_LEDGER_COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")
"""SQLite 伴随文件后缀；只允许主库存在时按固定名出现。"""

_DEPLOYMENT_EXTENSIONS = (".py", ".md", ".toml", ".txt", ".rst", ".yaml", ".yml", ".ini", ".cfg", ".pyc", ".pyo")
"""插件自身部署文件扩展名；带状态标记但属这些扩展名的条目不是账本证据（design D11）。"""

_STATE_NAME_MARKERS = ("ledger", "delivery", "courier", "snapshot", "receipt")
"""状态类证据名字标记（忽略大小写）；用于识别旧账本归档、导出与快照痕迹。"""

_CHINESE_STATE_MARKERS = ("快照", "归档", "备份")
"""中文状态标记；仅在不以部署类扩展名结尾时阻断。

刻意不含 ASCII `backup`/`archive`：真实部署目录存在 `config_back/`，按 ASCII
子串判定会把正常部署误判成历史证据（design D11 第 1 条）。
"""

_ARCHIVE_EXTENSIONS = (".tar", ".tgz", ".gz", ".zip", ".7z", ".rar", ".bak")
"""归档类扩展名；命中即阻断，不按部署文件放行（design D11 第 1 条）。"""

_TMP_EXPORT_NAME_PATTERN = re.compile(r"\.tmp-[0-9a-f]{8}\s*\Z")
"""历史导出/半成品的临时名形态 `<目标>.tmp-<8位十六进制>`（忽略大小写）。

`$` 只在「字符串末尾或结尾换行之前」匹配，`\\Z` 只认真正的字符串末尾：单用 `\\Z`
会漏掉以换行结尾的异常名字，因此这里用 `\\s*\\Z` 容忍结尾空白，使
`x.tmp-1234abcd\\n` 这类名字同样命中，不留下可按文件名差异绕过的空洞。
"""

_DATABASE_NAME_SUFFIXES = (".sqlite3", ".sqlite", ".db")
"""主库以外的数据库文件名后缀；带任一 SQLite 伴随后缀时按去掉后缀的名字判定。"""


class _StateLock:
    """非阻塞 flock；明确解锁与描述符关闭分别确认。"""

    def __init__(self, directory: Path) -> None:
        self._close_unconfirmed = False
        lock_path = directory / STATE_LOCK_FILE_NAME
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        self.created = False
        try:
            self.fd: int | None = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            self.created = True
        except FileExistsError:
            self.fd = os.open(lock_path, flags)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                raise OSError("锁文件来源不可确认")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                self.release()
            except LedgerUnavailableError:
                if self.fd is not None:
                    with _CLEANUP_MUTEX:
                        _PENDING_CLEANUP[directory / LEDGER_FILE_NAME] = (None, self)
                raise LedgerUnavailableError("无法取得本插件状态目录独占锁，清理未确认，关闭发送") from None
            raise LedgerUnavailableError("无法取得本插件状态目录独占锁，关闭发送") from None

    def release(self) -> None:
        """先解锁再关闭；关闭结果不明不重试号码，防止误关已复用的描述符。"""

        if self._close_unconfirmed:
            raise LedgerUnavailableError("独占锁已解锁，但描述符关闭未确认，须进程退出清理")
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                raise LedgerUnavailableError("独占锁释放未确认，保留句柄并关闭发送") from None
            descriptor, self.fd = self.fd, None
            try:
                os.close(descriptor)
            except OSError:
                self._close_unconfirmed = True
                raise LedgerUnavailableError("独占锁已解锁，但描述符关闭未确认，须进程退出清理") from None


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
class Reservation:
    """占位结果：granted 表示发送资格；record=None 仅表示评论没有对应源记录。

    `reserve` 始终返回已有或新建的源记录；`reserve_comment` 不为不存在的源创建记录。
    """

    granted: bool
    record: DeliveryRecord | None


LedgerOpener = Callable[[Path], Union["DeliveryLedger", LedgerUnavailable]]
"""账本打开方式（组合根注入点：生产用 `open_ledger`，测试可注入替身）。"""


_PENDING_CLEANUP: dict[Path, tuple[sqlite3.Connection | None, _StateLock]] = {}
_CLEANUP_MUTEX = RLock()
"""只保留打开失败时尚未释放的存储资源，不保存投递事实；下次打开先重试关闭。"""


def _close_storage(connection: sqlite3.Connection | None, lock: _StateLock | None) -> None:
    """只有连接明确关闭才释放独占；不泄漏存储异常原文。"""

    if connection is not None:
        try:
            connection.close()
        except (sqlite3.Error, OSError):
            raise LedgerUnavailableError("状态库连接关闭失败，独占使用权尚未释放") from None
    if lock is not None:
        lock.release()


def _is_database_name(name: str) -> bool:
    """名字是否形如数据库文件（含其 SQLite 伴随后缀形态，忽略大小写）。"""

    lowered = name.lower()
    for suffix in _LEDGER_COMPANION_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return lowered.endswith(_DATABASE_NAME_SUFFIXES)


def _state_evidence_category(name: str) -> str | None:
    """按条目**名字**识别状态类证据类别；不读内容、不区分文件与目录。

    部署类扩展名（源码/文档/配置/缓存）不因名字含标记而成为证据；主库、本插件
    独占锁与主库伴随后缀是固定状态文件，允许与否由调用方在名字规则之前处理。
    额外命中三类形态：`.tmp-<8位十六进制>` 历史导出临时名、归档类扩展名，以及
    不以部署类扩展名结尾的中文「快照/归档/备份」标记（design D11 第 1 条）。
    """

    lowered = name.lower()
    if lowered.startswith("service_binding.json"):
        return "服务切换收据"
    if _is_database_name(name):
        return "主库以外的数据库或归档"
    if _TMP_EXPORT_NAME_PATTERN.search(lowered) is not None:
        return "历史导出的临时文件"
    if lowered.endswith(_ARCHIVE_EXTENSIONS):
        return "归档类文件"
    if lowered.endswith(".lock"):
        return "其他进程的锁文件"
    if lowered.endswith(_DEPLOYMENT_EXTENSIONS):
        return None
    if any(marker in name for marker in _CHINESE_STATE_MARKERS) or any(
        marker in lowered for marker in _STATE_NAME_MARKERS
    ):
        return "带状态标记的未核对条目"
    return None


def _state_evidence_reason(entry: Path, *, main_exists: bool) -> str | None:
    """判定目录条目是否为须人工核对的状态类证据；只做名字与类型检查。

    允许：主库、本插件锁、主库存在时的 SQLite 伴随文件，以及源码、文档、配置、
    缓存等部署类常规文件/目录。阻断：链接、硬链接与非普通文件；服务收据；主库
    以外的数据库文件与归档副本；`.tmp-<8位十六进制>` 导出临时名；归档类扩展名；
    主库缺失时的孤立伴随文件；其他锁文件；带状态标记（含中文「快照/归档/备份」）
    的非部署条目；子目录自身或其直接子项名字命中上述规则。绝不读取内容、
    不递归多层、不修改任何文件。诊断只含安全条目名与类别。
    """

    name = entry.name
    info = entry.lstat()
    if stat.S_ISDIR(info.st_mode):
        category = _state_evidence_category(name)
        if category is not None:
            return f"状态目录内的子目录「{name}」属未核对状态类证据（{category}），须人工核对"
        for child in entry.iterdir():
            child_category = _state_evidence_category(child.name)
            if child_category is not None:
                return (
                    f"状态目录内的子目录「{name}」含未核对状态类证据"
                    f"（{child_category}：{child.name}），须人工核对"
                )
        return None
    if not stat.S_ISREG(info.st_mode):
        return f"状态目录条目「{name}」不是常规文件（链接或特殊文件），须人工核对"
    if info.st_nlink != 1:
        return f"状态目录条目「{name}」存在多个硬链接，须人工核对"
    if name in (LEDGER_FILE_NAME, STATE_LOCK_FILE_NAME):
        return None
    if name in {LEDGER_FILE_NAME + suffix for suffix in _LEDGER_COMPANION_SUFFIXES}:
        if main_exists:
            return None
        return f"主库缺失但存在孤立伴随文件「{name}」，须人工核对"
    category = _state_evidence_category(name)
    if category is not None:
        return f"状态目录内存在未核对状态类证据（{category}：{name}），须人工核对"
    return None


def open_ledger(path: Path, *, clock: Clock | None = None) -> DeliveryLedger | LedgerUnavailable:
    """先独占、识别目录历史、只读核验原库，再打开写连接并恢复未完成阶段。

    历史核对只阻断状态类证据（服务收据、主库以外的数据库与归档、导出临时名、
    归档扩展名、中文快照/归档/备份标记、孤立伴随文件、链接文件及其子目录内一层
    名字命中的同类证据），插件自身部署文件不阻断发送；只在本次新建锁且没有任何
    状态类证据时按新装建库。拒绝符号链接、硬链接、未知状态条目或不兼容库，
    不删除证据、不打开服务数据库（design D6/D11）。
    """

    time_source = clock or SystemClock()
    connection: sqlite3.Connection | None = None
    lock: _StateLock | None = None
    try:
        path = path.absolute()
        if path.name != LEDGER_FILE_NAME or ".." in path.parts:
            raise LedgerUnavailableError("状态库路径不符合本插件固定文件约定")
        for directory in reversed(path.parents):
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise LedgerUnavailableError("状态目录来源不可确认，拒绝使用符号链接")
        with _CLEANUP_MUTEX:
            pending = _PENDING_CLEANUP.get(path)
            if pending is not None:
                _close_storage(*pending)
                del _PENDING_CLEANUP[path]
        lock = _StateLock(path.parent)
        main_exists = path.exists()
        for entry in tuple(path.parent.iterdir()):
            reason = _state_evidence_reason(entry, main_exists=main_exists)
            if reason is not None:
                raise LedgerUnavailableError(reason)
        if main_exists:
            _validate_wal(path)
            connection = sqlite3.connect(
                f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            _validate_database(connection)
            connection.close()
            connection = sqlite3.connect(
                f"{path.absolute().as_uri()}?mode=rw", uri=True, timeout=5.0, isolation_level=None, check_same_thread=False
            )
        else:
            if not lock.created:
                raise LedgerUnavailableError("原账本缺失但存在上次运行的独占锁或状态类证据，须人工核对")
            connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
            connection.execute(
                f"""
                CREATE TABLE {_TABLE} (
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
        ledger = DeliveryLedger(connection, time_source, lock=lock)
        ledger.recover_interrupted()
    except BaseException as error:
        reason = error.reason if isinstance(error, LedgerUnavailableError) else _describe(error)
        try:
            _close_storage(connection, lock)
        except LedgerUnavailableError as cleanup_error:
            reason += "；" + cleanup_error.reason
            if lock is not None and lock.fd is not None:
                with _CLEANUP_MUTEX:
                    _PENDING_CLEANUP[path] = (connection, lock)
        if isinstance(error, (sqlite3.Error, OSError, LedgerUnavailableError)):
            return LedgerUnavailable(reason)
        raise
    return ledger


def _wal_checksum(data: bytes, order: str, checksum: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """SQLite WAL 的双字累计校验；存储校验值始终使用大端序。"""

    first, second = checksum
    for left, right in struct.iter_unpack(order + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def _validate_wal(path: Path) -> None:
    """在 SQLite 可能忽略坏日志或重建共享索引之前，只读核实全部 WAL 帧。

    格式依据 https://sqlite.org/fileformat.html#walformat（4.1–4.2）。
    完整合法 WAL 与空 WAL 可用；截断、校验不符或代次不明的尾部不能证明无成功
    事实，保留原文件并要求人工核实。按页读取，不复制数据库或建立迁移框架。
    """

    wal = path.with_name(path.name + "-wal")
    if not wal.exists():
        return
    reason = "状态库日志历史无法核实，保留原文件并关闭发送，须人工核对"
    with wal.open("rb") as stream:
        header = stream.read(32)
        if not header:
            return
        if len(header) != 32:
            raise LedgerUnavailableError(reason)
        magic, version, page_size, _, salt1, salt2, check1, check2 = struct.unpack(">8I", header)
        if (magic not in (0x377F0682, 0x377F0683) or version != 3007000
                or not 512 <= page_size <= 65536 or page_size & (page_size - 1)):
            raise LedgerUnavailableError(reason)
        order = "<" if magic == 0x377F0682 else ">"
        checksum = _wal_checksum(header[:24], order)
        if checksum != (check1, check2):
            raise LedgerUnavailableError(reason)
        with path.open("rb") as database:
            database_header = database.read(20)
        database_page_size = int.from_bytes(database_header[16:18], "big")
        if database_page_size == 1:
            database_page_size = 65536
        if (database_header[:16] != b"SQLite format 3\x00" or database_header[18:20] != b"\x02\x02"
                or database_page_size != page_size):
            raise LedgerUnavailableError(reason)
        while frame := stream.read(24):
            if len(frame) != 24:
                raise LedgerUnavailableError(reason)
            page, _, frame_salt1, frame_salt2, check1, check2 = struct.unpack(">6I", frame)
            content = stream.read(page_size)
            if (page == 0 or (frame_salt1, frame_salt2) != (salt1, salt2)
                    or len(content) != page_size):
                raise LedgerUnavailableError(reason)
            checksum = _wal_checksum(frame[:8], order, checksum)
            checksum = _wal_checksum(content, order, checksum)
            if checksum != (check1, check2):
                raise LedgerUnavailableError(reason)


def _validate_database(connection: sqlite3.Connection) -> None:
    """沿用迁移的完整性检查，严格验证原列、主键、状态且不修补未知结构。"""

    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise LedgerUnavailableError("状态库完整性检查失败，可能损坏，须人工核对")
    objects = connection.execute("SELECT type, name FROM sqlite_schema").fetchall()
    if set(objects) != {("table", _TABLE), ("index", "sqlite_autoindex_delivery_ledger_1")}:
        raise LedgerUnavailableError("状态库缺表或含未知结构，拒绝当作空历史")
    key_columns = ("account_id", "source_group_id", "source_message_id", "target_group_id")
    names = key_columns + LEDGER_COLUMNS
    columns = connection.execute(f"PRAGMA table_xinfo({_TABLE})").fetchall()
    expected = [
        (name, "REAL" if name in ("created_at", "updated_at") else "TEXT", 1,
         "''" if name in ("target_message_id", "comment_status", "comment_message_id", "reason") else None,
         index + 1 if index < 4 else 0, 0)
        for index, name in enumerate(names)
    ]
    if [tuple(row[1:]) for row in columns] != expected:
        raise LedgerUnavailableError("状态库列、类型或四字段主键与原 schema 不符，须人工核对")
    # table_xinfo 不包含比较规则；由 SQLite 给出真正参与唯一性判定的索引列。
    primary_key = connection.execute("PRAGMA index_xinfo(sqlite_autoindex_delivery_ledger_1)").fetchall()
    # SQLite 排序规则名只按 ASCII 大小写等价；不折叠未知 Unicode 名称。
    if [(row[1], row[2], row[4].upper() if isinstance(row[4], str) and row[4].isascii() else row[4])
        for row in primary_key if row[5]] != [
        (index, name, "BINARY") for index, name in enumerate(key_columns)
    ]:
        raise LedgerUnavailableError("状态库四字段主键比较规则与原 schema 不符，须人工核对")
    # 主键可显式覆盖列的 NOCASE/RTRIM，不能据 index_xinfo 推断 WHERE 的列规则。
    # 仅完整匹配原 DDL 与明确 BINARY 等价写法，不解析通用 SQL、不替换/执行库内文本。
    # 注释必须在第一个结束符停止，避免回溯吞掉真实的 COLLATE；未知结构一律关闭。
    gap = r"(?:\s|--[^\r\n]*(?:\r\n?|\n|$)|/\*(?:[^*]|\*(?!/))*\*/)*"

    def identifier(name: str) -> str:
        return rf"(?:{name}|\"{name}\"|`{name}`|\[{name}\]|'{name}')"

    binary = rf"(?:{gap}COLLATE{gap}{identifier('BINARY')})?"
    declarations = []
    for name, kind, _, default, _, _ in expected:
        declaration = gap.join((identifier(name), identifier(kind) + binary, "NOT", "NULL" + binary))
        if default is not None:
            declaration += gap + "DEFAULT" + gap + re.escape(default)
        declarations.append(declaration)
    separator = gap + "," + gap
    declarations.append(gap.join(("PRIMARY", "KEY", r"\(",
                                 separator.join(identifier(name) + binary for name in key_columns), r"\)")))
    pattern = gap + gap.join(("CREATE", "TABLE", identifier(_TABLE), r"\(",
                              separator.join(declarations), r"\)")) + gap
    ddl = connection.execute("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (_TABLE,)).fetchone()
    if ddl is None or not isinstance(ddl[0], str) or re.fullmatch(pattern, ddl[0], re.IGNORECASE | re.ASCII) is None:
        raise LedgerUnavailableError("状态库列及四字段主键比较规则无法确认为原 schema，须人工核对")
    states = (STATUS_SENDING, STATUS_SENT, STATUS_FAILED, STATUS_UNKNOWN)
    for row in connection.execute(f"SELECT {', '.join(names)} FROM {_TABLE}"):
        if (not all(isinstance(value, str) for value in row[:9])
                or not all(value.strip() for value in row[:4])
                or row[4] not in states or row[6] not in ("", *states)
                or (row[4] == STATUS_SENT and not row[5].strip())
                or (row[6] == STATUS_SENT and not row[7].strip())
                or (row[4] != STATUS_SENT and row[6] != "")
                or (row[6] == "" and row[7] != "")
                or not all(isinstance(value, (int, float)) for value in row[9:])):
            raise LedgerUnavailableError("状态库含不兼容记录，保留原历史并关闭发送")


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

    def __init__(self, connection: sqlite3.Connection, clock: Clock, *, lock: _StateLock | None = None) -> None:
        self._connection = connection
        self._clock = clock
        self._lock = lock
        self._mutex = RLock()
        self._unavailable = False

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
                granted = _single_row_changed(cursor)
                record = self._fetch(cursor, key)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from None
        if record is None:
            raise LedgerUnavailableError("投递占位登记后无法读回记录")
        return Reservation(granted=granted, record=record)

    def recover_interrupted(self) -> int:
        """恢复遗留源/评论 sending 为 unknown，返回受影响阶段数。

        `open_ledger` 已在独占且结构核对后自动调用；公开方法保留给启动兼容入口，
        没有在途工作时重复调用返回零。不得在活跃发送过程中调用。
        源与评论独立恢复，评论中断不能改变源 sent 或清空目标 ID。

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
                recovered = cursor.rowcount
                cursor.execute(
                    f"UPDATE {_TABLE} SET comment_status = ?, updated_at = ? "
                    "WHERE comment_status = ?",
                    (STATUS_UNKNOWN, self._clock.now(), STATUS_SENDING),
                )
                return recovered + cursor.rowcount
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from None

    def mark_sent(self, key: DeliveryKey, target_message_id: str) -> bool:
        """把在途占位登记为成功（保存真实目标消息 ID）。

        返回 `True` 表示已更新到在途占位；返回 `False` 表示没有可更新的 `sending`
        记录（占位未命中）；存储失败抛 `LedgerUnavailableError`。
        """

        if not isinstance(target_message_id, str) or not target_message_id.strip():
            raise LedgerUnavailableError("成功回执缺少目标消息 ID，保留在途占位")
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
        """登记明确未发送，允许显式新尝试；兼容接收 reason，但仅落盘安全类别。"""

        return self._finalize_status(key, STATUS_FAILED, reason)

    def mark_unknown(self, key: DeliveryKey, reason: str) -> bool:
        """登记待核实并阻止重发；兼容接收 reason，但仅落盘安全类别。"""

        return self._finalize_status(key, STATUS_UNKNOWN, reason)

    def reserve_comment(self, key: DeliveryKey) -> Reservation:
        """仅对源 sent 且评论空态原子授予一次 sending 占位，不创建缺失源行。"""

        try:
            with self._transaction() as cursor:
                cursor.execute(
                    f"UPDATE {_TABLE} SET comment_status = ?, updated_at = ? "
                    f"WHERE {_key_where()} AND status = ? AND comment_status = ''",
                    (STATUS_SENDING, self._clock.now(), *_key_values(key), STATUS_SENT),
                )
                granted = _single_row_changed(cursor)
                record = self._fetch(cursor, key)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from None
        return Reservation(granted=granted, record=record)

    def record_comment(self, key: DeliveryKey, *, status: LedgerStatus, message_id: str = "") -> bool:
        """仅终结已成功源行的评论在途占位；其他状态不覆盖，存储故障抛异常。"""

        if status not in (STATUS_SENT, STATUS_FAILED, STATUS_UNKNOWN):
            raise LedgerUnavailableError("评论终态不合法，保留在途占位")
        if not isinstance(message_id, str) or (status == STATUS_SENT and not message_id.strip()):
            raise LedgerUnavailableError("评论成功回执缺少目标消息 ID，保留在途占位")
        return self._write(
            f"""
            UPDATE {_TABLE}
            SET comment_status = ?, comment_message_id = ?, updated_at = ?
            WHERE {_key_where()} AND status = ? AND comment_status = ?
            """,
            (str(status), str(message_id), self._clock.now(), *_key_values(key), STATUS_SENT, STATUS_SENDING),
        )

    def get(self, key: DeliveryKey) -> DeliveryRecord | None:
        """读取记录快照：不存在返回 `None`；读取失败抛 `LedgerUnavailableError`。"""

        try:
            with self._transaction() as cursor:
                return self._fetch(cursor, key)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from None

    def close(self) -> None:
        """关闭连接（不删除任何数据）。"""

        with self._mutex:
            _close_storage(self._connection, self._lock)
            self._lock = None

    def _finalize_status(self, key: DeliveryKey, status: LedgerStatus, reason: str) -> bool:
        """把在途占位登记为终态；不持久化调用方可能夹带正文的自由原因。"""

        safe_reason = "明确未发送" if status == STATUS_FAILED else "发送结果待核实"
        return self._write(
            f"""
            UPDATE {_TABLE}
            SET status = ?, reason = ?, updated_at = ?
            WHERE {_key_where()}
              AND status = ?
            """,
            (str(status), safe_reason, self._clock.now(), *_key_values(key), STATUS_SENDING),
        )

    def _write(self, sql: str, parameters: tuple[object, ...]) -> bool:
        """执行一条更新：返回是否命中在途占位；存储失败抛 `LedgerUnavailableError`。"""

        try:
            with self._transaction() as cursor:
                cursor.execute(sql, parameters)
                return _single_row_changed(cursor)
        except (sqlite3.Error, OSError) as error:
            raise LedgerUnavailableError(_describe(error)) from None

    def _fetch(self, cursor: sqlite3.Cursor, key: DeliveryKey) -> DeliveryRecord | None:
        """在给定游标（同一事务）内读回记录。"""

        cursor.execute(
            f"SELECT {', '.join(LEDGER_COLUMNS)} FROM {_TABLE} WHERE {_key_where()}",
            _key_values(key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if cursor.fetchone() is not None:
            raise LedgerUnavailableError("状态库四字段键命中多条记录，关闭发送")
        return DeliveryRecord(key=key, **dict(zip(LEDGER_COLUMNS, row)))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """短事务：`BEGIN IMMEDIATE` → 提交/回滚；绝不跨越任何 `await`。

        BEGIN、事务体与 COMMIT 任一失败都显式回滚：连接若停在未结束事务，下一次
        `BEGIN` 会报 `cannot start a transaction within a transaction`，把一次
        存储故障扩散成整个账本不可用。游标创建与关闭统一放在 finally，任何分支
        都不泄漏游标。
        """

        with self._mutex:
            if self._unavailable:
                raise LedgerUnavailableError("状态库连接已停用，须关闭并重新核对后使用")
            cursor: sqlite3.Cursor | None = None
            try:
                cursor = self._connection.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._rollback_quietly()
                raise
            finally:
                if cursor is not None and not self._unavailable:
                    cursor.close()

    def _rollback_quietly(self) -> None:
        """最多两次回滚并核实事务结束；仍失败则停用，保留独占直至公开关闭。

        停用后只尝试关闭污染连接，不释放目录独占；连接关闭失败也不能恢复读写。
        清理异常只给出安全类别，不能泄露存储错误原文。
        """

        for _ in range(2):
            try:
                self._connection.rollback()
                if not self._connection.in_transaction:
                    return
            except (sqlite3.Error, OSError):
                continue
        self._unavailable = True
        try:
            _close_storage(self._connection, None)
        except LedgerUnavailableError:
            raise LedgerUnavailableError("状态库回滚失败且连接关闭未确认，已停用并保留独占") from None
        raise LedgerUnavailableError("状态库回滚失败，连接已关闭停用，独占保留至公开关闭") from None


def _single_row_changed(cursor: sqlite3.Cursor) -> bool:
    """在事务提交前确认最多修改一个键；异常行数交由事务回滚。"""

    if cursor.rowcount not in (0, 1):
        raise LedgerUnavailableError("状态库四字段键更新行数异常，关闭发送")
    return cursor.rowcount == 1


def _key_values(key: DeliveryKey) -> tuple[str, str, str, str]:
    """键值按固定列顺序展开（参数绑定用）。"""

    return (key.account_id, key.source_group_id, key.source_message_id, key.target_group_id)


def _key_where() -> str:
    """键匹配条件（列名固定，值全部走参数绑定）。"""

    return (
        "account_id COLLATE BINARY = ? AND source_group_id COLLATE BINARY = ? "
        "AND source_message_id COLLATE BINARY = ? AND target_group_id COLLATE BINARY = ?"
    )
