"""3.1 服务端独占账本与短事务边界（design D7/D9；旧 schema 事实来自第 1 组 1.6）。

- 同一状态目录的第二个打开者（含真实子进程）不能打开账本，发送保持关闭；
- 不同临时目录的账本互不干扰；
- BEGIN、事务体与 COMMIT 的故障注入都显式回滚，之后账本仍可继续安全读写；
- 损坏或 schema 不符的库返回账本不可用：不覆盖原文件，也不把读取失败当作
  「记录不存在」。

所有数据库都是 `tmp_path` 合成目录里的文件；「第二个进程」用真实子进程验证。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from service.ledger_store import (
    DELIVERY_TABLE,
    LEGACY_DELIVERY_COLUMNS,
    LEGACY_KEY_COLUMNS,
    META_SCHEMA_VERSION,
    META_TABLE,
    SCHEMA_VERSION,
    SERVICE_LEDGER_FILE_NAME,
    ServiceLedger,
    StateDatabase,
    open_state_database,
)
from shared.delivery import DeliveryKey, LedgerUnavailable, LedgerUnavailableError
from shared.ledger import SourceReservation

from tests.support import FakeClock

ROOT = Path(__file__).resolve().parent.parent


def _key(message_id: str = "m-source-1") -> DeliveryKey:
    return DeliveryKey(
        account_id="10000",
        source_group_id="10001",
        source_message_id=message_id,
        target_group_id="20002",
    )


def _open(state_dir: Path, *, clock: Any = None) -> ServiceLedger:
    """经公开打开入口建库并构造账本（不直接写 SQL 建表）。"""

    database = open_state_database(state_dir, writable=True, create=True)
    assert isinstance(database, StateDatabase), f"账本应可打开: {database!r}"
    return ServiceLedger(database.connection, clock or FakeClock(), lock=database.lock)


def _reserve(
    ledger: ServiceLedger,
    key: DeliveryKey,
    *,
    attempt_id: str = "attempt-1",
    request_id: str = "req-1",
    session_id: str = "sess-1",
) -> SourceReservation:
    return asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id=request_id,
            session_id=session_id,
            policy_generation=1,
        )
    )


def _close(ledger: ServiceLedger) -> None:
    """关闭账本（协议方法是 async：测试用独立事件循环驱动）。"""

    asyncio.run(ledger.close())


def _rows(state_dir: Path) -> list[tuple[str, str]]:
    """用独立连接观察落盘结果（不经账本对象，不依赖其私有结构）。"""

    connection = sqlite3.connect(
        str(state_dir / SERVICE_LEDGER_FILE_NAME), timeout=5.0, isolation_level=None
    )
    try:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                f"SELECT source_message_id, status FROM {DELIVERY_TABLE}"
            )
        ]
    finally:
        connection.close()


# --------------------------------------------------------------------------- 独占


def test_同一状态目录第二个打开者被拒绝且第一个不受影响(tmp_path: Path) -> None:
    first = _open(tmp_path)
    try:
        second = open_state_database(tmp_path, writable=True, create=False)
        assert isinstance(second, LedgerUnavailable), f"同一目录必须只允许一个打开者: {second!r}"
        assert "独占" in second.reason or "另一个" in second.reason, second.reason

        granted = _reserve(first, _key())
        assert granted.granted is True, "独占锁不得影响持有者自身可用性"
        assert _rows(tmp_path) == [("m-source-1", "sending")]
    finally:
        _close(first)

    reopened = _open(tmp_path)
    try:
        assert _reserve(reopened, _key("m-after-reopen"), attempt_id="attempt-2").granted is True, (
            "释放后必须可以再次打开"
        )
    finally:
        _close(reopened)


_HOLDER_CODE = """
import json, sys
from pathlib import Path
from service.ledger_store import open_state_database

database = open_state_database(Path(sys.argv[1]), writable=True, create=True)
if not hasattr(database, "connection"):
    print(json.dumps({"ready": False, "reason": database.reason}), flush=True)
    raise SystemExit(1)
print(json.dumps({"ready": True}), flush=True)
sys.stdin.readline()
database.close()
print(json.dumps({"closed": True}), flush=True)
"""


_PROBER_CODE = """
import json, sys
from pathlib import Path
from service.ledger_store import open_state_database

outcome = open_state_database(Path(sys.argv[1]), writable=True, create=False)
print(
    json.dumps(
        {
            "opened": hasattr(outcome, "connection"),
            "reason": getattr(outcome, "reason", ""),
        }
    ),
    flush=True,
)
"""


def _child_env() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def test_第二进程不能打开同一数据目录的账本(tmp_path: Path) -> None:
    """真实子进程验证互斥：持有者活着时，第二个进程必须被拒绝。"""

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_CODE, str(tmp_path)],
        cwd=ROOT,
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdin is not None
        ready = json.loads(holder.stdout.readline())
        assert ready == {"ready": True}, (ready, holder.stderr.read() if holder.stderr else "")

        prober = subprocess.run(
            [sys.executable, "-c", _PROBER_CODE, str(tmp_path)],
            cwd=ROOT,
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert prober.returncode == 0, prober.stderr
        outcome = json.loads(prober.stdout.strip())
        assert outcome["opened"] is False, "第二个进程不得打开被独占的账本"
        assert outcome["reason"], "拒绝必须给出可定位原因"
    finally:
        try:
            if holder.stdin is not None:
                holder.stdin.write("done\n")
                holder.stdin.flush()
        except OSError:
            pass
        holder.wait(timeout=60)


def test_不同临时目录的账本互不干扰(tmp_path: Path) -> None:
    first_dir = tmp_path / "state-a"
    second_dir = tmp_path / "state-b"
    first = _open(first_dir)
    second = _open(second_dir)
    try:
        assert _reserve(first, _key("m-a")).granted is True
        assert _reserve(second, _key("m-b")).granted is True

        assert _rows(first_dir) == [("m-a", "sending")], "另一个目录的记录不得出现在本目录"
        assert _rows(second_dir) == [("m-b", "sending")]
    finally:
        _close(first)
        _close(second)


# ------------------------------------------------------------------- 短事务边界


class FaultyCursor(sqlite3.Cursor):
    """可注入一次语句失败的游标：用于 BEGIN 与事务体故障注入。"""

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        failing = getattr(self.connection, "failing_statement", None)
        statement = str(args[0]) if args else ""
        if failing and failing in statement:
            setattr(self.connection, "failing_statement", None)
            raise sqlite3.OperationalError(f"注入的语句失败：{failing}")
        return super().execute(*args, **kwargs)


class FaultyConnection(sqlite3.Connection):
    """注入式故障连接：语句与提交各可失败一次，其余行为与真实连接一致。"""

    failing_statement: str | None = None
    failing_commit: bool = False

    def fail_next_statement(self, contains: str) -> None:
        self.failing_statement = contains

    def fail_next_commit(self) -> None:
        self.failing_commit = True

    def cursor(self, factory: Any = FaultyCursor) -> sqlite3.Cursor:
        return super().cursor(factory)

    def commit(self) -> None:
        if self.failing_commit:
            self.failing_commit = False
            raise sqlite3.OperationalError("注入的提交失败")
        super().commit()


def _faulty_ledger(tmp_path: Path) -> tuple[ServiceLedger, FaultyConnection]:
    """先经公开入口建好 schema，再用可注入故障的连接构造账本。"""

    _close(_open(tmp_path))
    connection = sqlite3.connect(
        str(tmp_path / SERVICE_LEDGER_FILE_NAME),
        timeout=5.0,
        isolation_level=None,
        factory=FaultyConnection,
    )
    assert isinstance(connection, FaultyConnection)
    return ServiceLedger(connection, FakeClock()), connection


@pytest.mark.parametrize(
    "injection",
    ["BEGIN", "INSERT INTO delivery_ledger", "COMMIT"],
)
def test_BEGIN事务体与COMMIT故障都回滚且账本继续可用(
    tmp_path: Path, injection: str
) -> None:
    ledger, connection = _faulty_ledger(tmp_path)
    try:
        if injection == "COMMIT":
            connection.fail_next_commit()
        else:
            connection.fail_next_statement(injection)

        with pytest.raises(LedgerUnavailableError) as failure:
            _reserve(ledger, _key("m-fault"))
        assert str(failure.value).strip(), f"{injection} 故障必须如实报告不可用"

        assert _rows(tmp_path) == [], f"{injection} 故障必须回滚，不得留下半条记录"

        resumed = _reserve(ledger, _key("m-after-fault"), attempt_id="attempt-2")
        assert resumed.granted is True, f"{injection} 故障后账本必须继续可用（连接不得停在事务里）"
        assert _rows(tmp_path) == [("m-after-fault", "sending")]
    finally:
        _close(ledger)


# --------------------------------------------------------------------- 不可用库


def test_损坏库返回账本不可用且不覆盖原文件(tmp_path: Path) -> None:
    path = tmp_path / SERVICE_LEDGER_FILE_NAME
    broken = "这不是一个 SQLite 数据库".encode("utf-8") + b"\x00\xff" * 8
    path.write_bytes(broken)

    outcome = open_state_database(tmp_path, writable=True, create=True)

    assert isinstance(outcome, LedgerUnavailable), f"损坏库必须返回不可用: {outcome!r}"
    assert "损坏" in outcome.reason, outcome.reason
    assert path.read_bytes() == broken, "不得删除、覆盖或改写损坏的状态数据"


def test_旧库改名冒充服务账本时按schema不符拒绝(tmp_path: Path) -> None:
    """旧插件账本只有旧列；服务账本要求少一列都不行（必须显式 migrate）。"""

    legacy = tmp_path / SERVICE_LEDGER_FILE_NAME
    connection = sqlite3.connect(str(legacy), isolation_level=None)
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
        connection.execute(
            f"INSERT INTO {DELIVERY_TABLE} VALUES ('10000','10001','m-old','20002','sent','x','','','',1,2)"
        )
    finally:
        connection.close()
    before = legacy.read_bytes()

    outcome = open_state_database(tmp_path, writable=True, create=False)

    assert isinstance(outcome, LedgerUnavailable), f"旧 schema 不得当成本服务账本: {outcome!r}"
    assert "schema" in outcome.reason or "迁移" in outcome.reason, outcome.reason
    assert legacy.read_bytes() == before, "拒绝时必须原样保留既有数据"


def test_读取失败与记录不存在可区分(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        assert asyncio.run(ledger.lookup_delivery("f" * 64)) is None, (
            "记录不存在必须返回 None，不是不可用"
        )
    finally:
        _close(ledger)

    with pytest.raises(LedgerUnavailableError) as failure:
        asyncio.run(ledger.lookup_delivery("f" * 64))
    assert str(failure.value).strip(), "读取失败必须抛 LedgerUnavailableError，不得当作不存在"


def test_相对状态目录的只读入口可正常工作(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读入口（`ledger status` 用的 `mode=ro`）必须接受相对状态目录，不得崩溃。"""

    from service.ledger_migration import read_ledger_status

    _close(_open(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    report = read_ledger_status(Path("state"))

    assert hasattr(report, "ledger_identity"), report
    assert report.initialized is False, "未初始化的账本只读报告必须明确「未初始化」"


def test_服务端投递表保持旧库列与四字段主键(tmp_path: Path) -> None:
    """投递表必须保留旧库列名与四字段主键：旧公共入口与迁移/导出按此契约工作。"""

    ledger = _open(tmp_path)
    _close(ledger)

    connection = sqlite3.connect(
        str(tmp_path / SERVICE_LEDGER_FILE_NAME), isolation_level=None
    )
    try:
        info = [
            (str(row[1]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({DELIVERY_TABLE})")
        ]
    finally:
        connection.close()

    names = [name for name, _ in info]
    for column in LEGACY_DELIVERY_COLUMNS:
        assert column in names, f"投递表必须保留旧库列 {column}"
    primary = [name for name, position in sorted(info, key=lambda item: item[1]) if position]
    assert primary == list(LEGACY_KEY_COLUMNS), "防重主键必须恰为账号+源群+源消息+目标群"


def test_schema版本不符的账本按不可用拒绝(tmp_path: Path) -> None:
    """模块承诺「schema 版本不符即不可用」：实现必须校验元数据里的版本。

    `create` 路径允许版本为空（首次初始化稍后写入）；已写入但不等于当前版本时，
    读写两条路径都必须拒绝，且不改写文件。
    """

    from service.ledger_migration import initialize_fresh, read_ledger_status

    fresh = initialize_fresh(tmp_path, clock=FakeClock())
    assert hasattr(fresh, "ledger_identity"), fresh
    path = tmp_path / SERVICE_LEDGER_FILE_NAME

    other_version = "0" if SCHEMA_VERSION != "0" else "99"
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        connection.execute(
            f"UPDATE {META_TABLE} SET value = ? WHERE key = ?",
            (other_version, META_SCHEMA_VERSION),
        )
    finally:
        connection.close()
    tampered = path.read_bytes()

    outcome = open_state_database(tmp_path, writable=True, create=False)
    assert isinstance(outcome, LedgerUnavailable), "schema 版本不符必须按不可用拒绝"
    assert "版本" in outcome.reason, outcome.reason

    report = read_ledger_status(tmp_path)
    assert isinstance(report, LedgerUnavailable), "只读报告同样不得把版本不符当作可用账本"
    assert "版本" in report.reason, report.reason

    assert path.read_bytes() == tampered, "拒绝时不得改写账本文件"


def test_投递ID查询有索引且不退化全表扫描(tmp_path: Path) -> None:
    """账本永不清理：`delivery_id` 查找必须有索引，避免随记录增长退化为全表扫描。"""

    _close(_open(tmp_path))
    path = tmp_path / SERVICE_LEDGER_FILE_NAME

    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        indexes = [str(row[1]) for row in connection.execute(f"PRAGMA index_list({DELIVERY_TABLE})")]
        assert indexes, "投递表必须带 delivery_id 索引"
        indexed: list[str] = []
        for name in indexes:
            columns = [str(row[2]) for row in connection.execute(f"PRAGMA index_info({name})")]
            if columns == ["delivery_id"]:
                indexed.append(name)
        assert indexed, f"缺少 delivery_id 索引：{indexes}"

        plan = list(
            connection.execute(
                f"EXPLAIN QUERY PLAN SELECT status FROM {DELIVERY_TABLE} WHERE delivery_id = ?",
                ("d" * 64,),
            )
        )
    finally:
        connection.close()

    detail = " ".join(str(row[3]) for row in plan)
    assert "USING INDEX" in detail.upper() or "USING COVERING INDEX" in detail.upper(), detail
