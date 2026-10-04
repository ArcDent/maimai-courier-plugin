"""第 4 组账本行为：经持久状态模块的**导出接口**驱动，不测试私有 SQL 辅助函数。

用例只断言账本对外承诺的行为：唯一键构成、四种状态的区分、评论结果附带、参数
绑定与持久化、事务纪律与失败可区分性。启动恢复（遗留 sending）与损坏库安全关闭
见本文件后续小节。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from delivery_ledger import (
    LEDGER_FILE_NAME,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    DeliveryLedger,
    LedgerStatus,
    LedgerUnavailable,
    LedgerUnavailableError,
    Reservation,
    open_ledger,
)

from tests.support import FakeClock, ledger_key, open_test_ledger


# --------------------------------------------------------------------------- 5.1


def test_独占失败不恢复活实例且不同目录隔离并可安全重开(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    other_dir = tmp_path / "other"
    first_dir.mkdir()
    other_dir.mkdir()
    first = open_test_ledger(first_dir)
    second = None
    other = None
    try:
        key = ledger_key()
        first.reserve(key)
        second = open_ledger(first_dir / LEDGER_FILE_NAME)
        assert isinstance(second, LedgerUnavailable)
        assert "独占" in second.reason
        assert str(first_dir) not in second.reason
        assert first.get(key).status == STATUS_SENDING
        other = open_test_ledger(other_dir)
        assert other.reserve(key).granted is True
        first.mark_sent(key, "target-1")
    finally:
        first.close()
        if isinstance(second, DeliveryLedger):
            second.close()
        if other is not None:
            other.close()
    reopened = open_test_ledger(first_dir)
    try:
        first.close()  # 旧句柄重复关闭不得释放新实例的锁。
        assert reopened.get(key).target_message_id == "target-1"
        blocked = open_ledger(first_dir / LEDGER_FILE_NAME)
        try:
            assert isinstance(blocked, LedgerUnavailable)
        finally:
            if isinstance(blocked, DeliveryLedger):
                blocked.close()
    finally:
        reopened.close()


def test_锁存储故障只有安全诊断且不建库(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    def fail_lock(*args: Any) -> None:
        raise OSError("私有路径/假凭证/源正文")

    monkeypatch.setattr(fcntl, "flock", fail_lock)
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    try:
        assert isinstance(result, LedgerUnavailable)
        assert result.reason and "假凭证" not in result.reason
        assert "私有路径" not in result.reason and "源正文" not in result.reason
        assert not (tmp_path / LEDGER_FILE_NAME).exists()
    finally:
        if isinstance(result, DeliveryLedger):
            result.close()


# --------------------------------------------------------------------------- 5.2


# 独立按原 schema 构造输入，不能从待测实现获取 DDL 当预期。
LEGACY_SCHEMA = """
CREATE TABLE delivery_ledger (
    account_id TEXT NOT NULL, source_group_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL, target_group_id TEXT NOT NULL,
    status TEXT NOT NULL, target_message_id TEXT NOT NULL DEFAULT '',
    comment_status TEXT NOT NULL DEFAULT '', comment_message_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)
)
"""


def _legacy_database(path: Path, *, schema: str = LEGACY_SCHEMA, status: str = "sent",
                     comment_status: str = "sent", page_size: int = 4096) -> None:
    """合成旧库；只给公共打开入口准备外部存储输入，不查询实现私有字段。"""

    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA page_size={page_size}")
        connection.execute(schema)
        connection.execute(
            "INSERT INTO delivery_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("10000", "10001", "m-source-1", "20002", status, "old-target",
             comment_status, "old-comment" if comment_status else "", "旧安全原因", 10.0, 20.0),
        )
        connection.commit()
    finally:
        connection.close()


def test_完整原库字段逐项兼容且不会重建(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path)
    before = path.read_bytes()
    ledger = open_test_ledger(tmp_path)
    try:
        record = ledger.get(ledger_key())
        assert record is not None
        assert (record.status, record.target_message_id, record.comment_status,
                record.comment_message_id, record.reason, record.created_at, record.updated_at) == (
                    "sent", "old-target", "sent", "old-comment", "旧安全原因", 10.0, 20.0)
        assert not ledger.reserve(ledger_key()).granted
    finally:
        ledger.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["空文件", "缺表", "缺列", "多列", "错主键", "错类型", "可空键",
                                  "未知源状态", "未知评论状态", "额外表", "触发器"])
def test_结构或状态不兼容不当空库修复(tmp_path: Path, kind: str) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    if kind == "空文件":
        path.touch()
    elif kind == "缺表":
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (id TEXT)")
        connection.close()
    else:
        schema = LEGACY_SCHEMA
        if kind == "缺列":
            schema = schema.replace("reason TEXT NOT NULL DEFAULT '', ", "")
        elif kind == "多列":
            schema = schema.replace("status TEXT NOT NULL,", "extra TEXT, status TEXT NOT NULL,", 1)
        elif kind == "错主键":
            schema = schema.replace("PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)",
                                    "PRIMARY KEY (source_message_id)")
        elif kind == "错类型":
            schema = schema.replace("account_id TEXT", "account_id INTEGER")
        elif kind == "可空键":
            schema = schema.replace("account_id TEXT NOT NULL", "account_id TEXT")
        if kind in {"未知源状态", "未知评论状态"}:
            _legacy_database(path, status="private-value" if kind == "未知源状态" else "sent",
                             comment_status="private-value" if kind == "未知评论状态" else "sent")
        else:
            connection = sqlite3.connect(path)
            connection.execute(schema)
            if kind == "额外表":
                connection.execute("CREATE TABLE ledger_meta (value TEXT)")
            if kind == "触发器":
                connection.execute("CREATE TRIGGER alter_facts AFTER INSERT ON delivery_ledger "
                                   "BEGIN DELETE FROM delivery_ledger; END")
            connection.close()
    before = path.read_bytes()
    result = open_ledger(path)
    try:
        assert isinstance(result, LedgerUnavailable)
        assert "private-value" not in result.reason
    finally:
        if isinstance(result, DeliveryLedger):
            result.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("collation", ["BINARY", "NOCASE", "RTRIM"])
@pytest.mark.parametrize("location", ["列", "主键"])
def test_原库四字段键必须保持逐字比较语义(tmp_path: Path, collation: str, location: str) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    if location == "列":
        schema = LEGACY_SCHEMA.replace("source_message_id TEXT NOT NULL",
                                       f"source_message_id TEXT COLLATE {collation} NOT NULL")
    else:
        schema = LEGACY_SCHEMA.replace("source_message_id, target_group_id)",
                                       f"source_message_id COLLATE {collation}, target_group_id)")
    _legacy_database(path, schema=schema)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE delivery_ledger SET source_message_id=?", ("m-CASE",))
    connection.commit()
    connection.close()
    before = path.read_bytes()
    ledger = open_ledger(path)
    try:
        if collation == "BINARY":
            assert isinstance(ledger, DeliveryLedger)
            assert ledger.get(ledger_key(message_id="m-CASE")).target_message_id == "old-target"
            for message_id in ("m-case", "m-CASE "):
                assert ledger.get(ledger_key(message_id=message_id)) is None
                assert ledger.reserve(ledger_key(message_id=message_id)).granted
            assert not ledger.reserve(ledger_key(message_id="m-CASE")).granted
        else:
            assert isinstance(ledger, LedgerUnavailable)
            assert "主键" in ledger.reason
            assert path.read_bytes() == before
    finally:
        if isinstance(ledger, DeliveryLedger):
            ledger.close()


@pytest.mark.parametrize("field", ["account_id", "source_group_id", "source_message_id", "target_group_id"])
@pytest.mark.parametrize("location", ["列", "主键"])
@pytest.mark.parametrize("collation", ["BINARY", "binary", "BiNaRy", '"binary"', '"BiNaRy"',
                                      "'BiNaRy'", "`BiNaRy`", "[BiNaRy]"])
def test_BINARY大小写等价原库可打开且四键源与评论逐字隔离(tmp_path: Path, field: str,
                                                        location: str, collation: str) -> None:
    fields = ("account_id", "source_group_id", "source_message_id", "target_group_id")
    path = tmp_path / LEDGER_FILE_NAME
    if location == "列":
        schema = LEGACY_SCHEMA.replace(f"{field} TEXT NOT NULL", f"{field} TEXT COLLATE {collation} NOT NULL")
    else:
        schema = LEGACY_SCHEMA.replace(
            "PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)",
            "PRIMARY KEY (" + ", ".join(f"{name} COLLATE {collation}" if name == field else name
                                        for name in fields) + ")")
    first = replace(ledger_key(), **{field: "CaseID"})
    second = replace(first, **{field: "caseid"})
    connection = sqlite3.connect(path)
    try:
        connection.execute(schema)
        connection.executemany(
            "INSERT INTO delivery_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(getattr(key, name) for name in fields) +
             ("sent", target, "", "", "旧安全原因", 10.0, 20.0)
             for key, target in ((first, "target-known"), (second, "right-target"))],
        )
        connection.commit()
    finally:
        connection.close()
    clock = FakeClock(start=100)
    ledger = open_ledger(path, clock=clock)
    try:
        assert isinstance(ledger, DeliveryLedger)
        original_first, original_second = ledger.get(first), ledger.get(second)
        assert original_first is not None and original_second is not None
        assert (original_first.status, original_first.target_message_id) == ("sent", "target-known")
        assert (original_second.status, original_second.target_message_id) == ("sent", "right-target")
        assert ledger.reserve(first) == Reservation(False, original_first)
        assert ledger.reserve(second) == Reservation(False, original_second)
        trailing = replace(first, **{field: "CaseID "})
        assert ledger.get(trailing) is None
        assert ledger.reserve_comment(trailing) == Reservation(False, None)
        assert ledger.reserve(trailing).granted
        comment = ledger.reserve_comment(second)
        assert comment.granted
        assert comment.record == replace(original_second, comment_status="sending", updated_at=100)
        assert not ledger.reserve_comment(second).granted
        assert ledger.get(first) == original_first
        assert ledger.get(second) == comment.record
    finally:
        if isinstance(ledger, DeliveryLedger):
            ledger.close()
    reopened = open_ledger(path, clock=clock)
    try:
        assert isinstance(reopened, DeliveryLedger)
        assert reopened.get(first) == original_first
        assert reopened.get(second) == replace(original_second, comment_status="unknown", updated_at=100)
        assert not reopened.reserve(first).granted
        assert not reopened.reserve(second).granted
        assert not reopened.reserve_comment(second).granted
    finally:
        if isinstance(reopened, DeliveryLedger):
            reopened.close()


@pytest.mark.parametrize("location", ["列", "主键"])
@pytest.mark.parametrize("collation", ["BıNARY", "BİNARY", "ＢＩＮＡＲＹ", "BINARY_UNKNOWN"])
def test_BINARY大小写兼容不接受Unicode易混淆或未知规则(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      location: str, collation: str) -> None:
    connect = sqlite3.connect

    def connect_with_collation(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        # 合成可被 SQLite 识别的未知名称，避免仅因缺少规则而提前失败。
        connection.create_collation(collation, lambda left, right: (left > right) - (left < right))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_with_collation)
    if location == "列":
        schema = LEGACY_SCHEMA.replace("source_message_id TEXT NOT NULL",
                                       f'source_message_id TEXT COLLATE "{collation}" NOT NULL')
    else:
        schema = LEGACY_SCHEMA.replace("source_message_id, target_group_id)",
                                       f'source_message_id COLLATE "{collation}", target_group_id)')
    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path, schema=schema)
    before = path.read_bytes()
    ledger = open_ledger(path)
    try:
        assert isinstance(ledger, LedgerUnavailable)
    finally:
        if isinstance(ledger, DeliveryLedger):
            ledger.close()
    assert path.read_bytes() == before


# 独立字面量列声明；主键故意覆盖为 BINARY，以暴露列默认规则与索引不一致。
def _collation_database(path: Path, field: str, collation: str, counterpart: str,
                        *, decorated: bool = False) -> tuple[DeliveryKey, DeliveryKey, list[tuple[Any, ...]]]:
    fields = ("account_id", "source_group_id", "source_message_id", "target_group_id")
    declarations = [f"{name} TEXT NOT NULL" for name in fields]
    comparison = (f'cOlLaTe /* BINARY 只是注释 */ "{collation.lower()}"'
                  if decorated else f"COLLATE {collation}")
    declarations[fields.index(field)] = f'"{field}" TEXT {comparison} NOT NULL'
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"""
            CREATE TABLE delivery_ledger (
                {', '.join(declarations)},
                status TEXT NOT NULL, target_message_id TEXT NOT NULL DEFAULT '',
                comment_status TEXT NOT NULL DEFAULT '', comment_message_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (account_id COLLATE BINARY, source_group_id COLLATE BINARY,
                             source_message_id COLLATE BINARY, target_group_id COLLATE BINARY)
            )
        """)
        first = replace(ledger_key(), **{field: "CaseID"})
        second = replace(first, **{field: counterpart})
        rows = [tuple(getattr(key, name) for name in fields) +
                ("sent", target, "", "", "", 10.0, 20.0)
                for key, target in ((first, "target-known"), (second, "right-target"))]
        connection.executemany("INSERT INTO delivery_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
        return first, second, rows
    finally:
        connection.close()


@pytest.mark.parametrize("field", ["account_id", "source_group_id", "source_message_id", "target_group_id"])
@pytest.mark.parametrize("collation,counterpart", [("NOCASE", "caseid"), ("RTRIM", "CaseID "),
                                                   ("BINARY", "caseid"), ("BINARY", "CaseID ")])
@pytest.mark.parametrize("decorated", [False, True])
def test_列比较规则不能被主键BINARY覆盖掩盖(tmp_path: Path, field: str, collation: str,
                                         counterpart: str, decorated: bool) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    first, second, rows = _collation_database(path, field, collation, counterpart, decorated=decorated)
    before = path.read_bytes()
    ledger = open_ledger(path)
    try:
        if collation == "BINARY":
            assert isinstance(ledger, DeliveryLedger)
            assert ledger.get(first).target_message_id == "target-known"
            assert ledger.get(second).target_message_id == "right-target"
            assert not ledger.reserve(first).granted
            assert not ledger.reserve(second).granted
        else:
            assert isinstance(ledger, LedgerUnavailable)
    finally:
        if isinstance(ledger, DeliveryLedger):
            ledger.close()
    assert path.read_bytes() == before
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT * FROM delivery_ledger ORDER BY rowid").fetchall() == rows
    finally:
        connection.close()


@pytest.mark.parametrize("field", ["account_id", "source_group_id", "source_message_id", "target_group_id"])
@pytest.mark.parametrize("collation,counterpart", [("NOCASE", "caseid"), ("RTRIM", "CaseID ")])
@pytest.mark.parametrize("operation", ["get", "reserve", "mark_sent", "mark_failed", "mark_unknown",
                                        "reserve_comment", "record_comment"])
def test_公开构造注入混合比较库也不能串读串写(tmp_path: Path, field: str, collation: str,
                                           counterpart: str, operation: str) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    first, second, _ = _collation_database(path, field, collation, counterpart)
    connection = sqlite3.connect(path, isolation_level=None)
    status = "failed" if operation == "reserve" else "sending" if operation.startswith("mark_") else "sent"
    connection.execute("UPDATE delivery_ledger SET status=?, comment_status=?",
                       (status, "sending" if operation == "record_comment" else ""))
    # 公开构造器注入存储，专门检验纵深防御；正式 open_ledger 必须拒绝此库。
    ledger = DeliveryLedger(connection, FakeClock())
    try:
        untouched = ledger.get(first)
        operations = {
            "get": lambda: ledger.get(second),
            "reserve": lambda: ledger.reserve(second),
            "mark_sent": lambda: ledger.mark_sent(second, "new-target"),
            "mark_failed": lambda: ledger.mark_failed(second, "明确未发"),
            "mark_unknown": lambda: ledger.mark_unknown(second, "待核实"),
            "reserve_comment": lambda: ledger.reserve_comment(second),
            "record_comment": lambda: ledger.record_comment(second, status="sent", message_id="comment-target"),
        }
        result = operations[operation]()
        if operation == "get":
            assert result.target_message_id == "right-target"
        elif operation in {"reserve", "reserve_comment"}:
            assert result.granted
            assert result.record.key == second
            if operation == "reserve_comment":
                assert result.record.target_message_id == "right-target"
                assert result.record.comment_status == "sending"
            else:
                assert result.record.status == "sending"
        else:
            assert result is True
        assert ledger.get(first) == untouched
        assert not connection.in_transaction
    finally:
        ledger.close()


@pytest.mark.parametrize("operation", ["get", "mark_sent", "mark_failed", "mark_unknown",
                                        "reserve_comment", "record_comment"])
def test_公开方法遇到异常多行必须回滚报不可用(tmp_path: Path, operation: str) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    schema = LEGACY_SCHEMA.replace(
        "PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)", "CHECK (1)")
    status = "sending" if operation.startswith("mark_") else "sent"
    _legacy_database(path, schema=schema, status=status,
                     comment_status="sending" if operation == "record_comment" else "")
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("INSERT INTO delivery_ledger SELECT * FROM delivery_ledger")
    before = path.read_bytes()
    rows = connection.execute("SELECT * FROM delivery_ledger ORDER BY rowid").fetchall()
    ledger = DeliveryLedger(connection, FakeClock())
    key = ledger_key()
    operations = {
        "get": lambda: ledger.get(key),
        "mark_sent": lambda: ledger.mark_sent(key, "new-target"),
        "mark_failed": lambda: ledger.mark_failed(key, "明确未发"),
        "mark_unknown": lambda: ledger.mark_unknown(key, "待核实"),
        "reserve_comment": lambda: ledger.reserve_comment(key),
        "record_comment": lambda: ledger.record_comment(key, status="sent", message_id="comment-target"),
    }
    try:
        with pytest.raises(LedgerUnavailableError):
            operations[operation]()
        assert not connection.in_transaction
        assert connection.execute("SELECT * FROM delivery_ledger ORDER BY rowid").fetchall() == rows
    finally:
        ledger.close()
    assert path.read_bytes() == before


def test_缺主库但有旧独占锁不允许当新装(tmp_path: Path) -> None:
    (tmp_path / "delivery_ledger.lock").touch()
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    try:
        assert isinstance(result, LedgerUnavailable)
        assert not (tmp_path / LEDGER_FILE_NAME).exists()
    finally:
        if isinstance(result, DeliveryLedger):
            result.close()


# --------------------------------------------------------------------------- 5.3


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("evidence", ["service_binding.json", "service_binding.json.tmp", "service_ledger.sqlite3",
                                      "service.lock", "旧账本快照.sqlite3", "export.tmp-1234abcd",
                                      "人工任意快照", "历史快照目录"])
def test_本目录历史证据不读内容不联网也不修改(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                existing: bool, evidence: str) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    if existing:
        _legacy_database(path)
    before = path.read_bytes() if existing else None
    marker = tmp_path / evidence
    if evidence == "历史快照目录":
        marker.mkdir()
        (marker / "delivery_ledger.sqlite3.bak").write_text("假敏感正文")
    else:
        marker.write_bytes(b"\xff damaged-or-secret")
    calls = []

    def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("历史未核对前不得打开任何数据库")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    result = open_ledger(path)
    assert isinstance(result, LedgerUnavailable)
    assert calls == []
    # 诊断只含安全条目名与类别，不回显条目正文（design D11）。
    assert "假敏感正文" not in result.reason and "damaged-or-secret" not in result.reason
    assert (path.read_bytes() if existing else None) == before
    assert path.exists() is existing
    if marker.is_file():
        assert marker.read_bytes() == b"\xff damaged-or-secret"
    else:
        assert (marker / "delivery_ledger.sqlite3.bak").read_text() == "假敏感正文"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_孤留伴随文件保持关闭且保留证据(tmp_path: Path, suffix: str) -> None:
    marker = tmp_path / (LEDGER_FILE_NAME + suffix)
    marker.write_bytes(b"synthetic-evidence")
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    assert isinstance(result, LedgerUnavailable)
    assert not (tmp_path / LEDGER_FILE_NAME).exists()
    assert marker.read_bytes() == b"synthetic-evidence"


@pytest.mark.parametrize("kind", ["数据库", "锁", "伴随文件", "目录", "祖先目录", "硬链接", "悬空链接", "命名管道"])
def test_无法确认来源的路径在打开数据库前拒绝(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    import os

    actual = tmp_path / "actual"
    actual.mkdir()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _legacy_database(actual / LEDGER_FILE_NAME)
    before = (actual / LEDGER_FILE_NAME).read_bytes()
    path = plugin / LEDGER_FILE_NAME
    if kind in {"数据库", "悬空链接"}:
        path.symlink_to(actual / (LEDGER_FILE_NAME if kind == "数据库" else "missing"))
    elif kind == "硬链接":
        os.link(actual / LEDGER_FILE_NAME, path)
    elif kind == "命名管道":
        os.mkfifo(path)
    elif kind in {"锁", "伴随文件"}:
        _legacy_database(path)
        (plugin / ("delivery_ledger.lock" if kind == "锁" else LEDGER_FILE_NAME + "-wal")).symlink_to(
            actual / LEDGER_FILE_NAME)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        if kind == "祖先目录":
            (actual / "child").mkdir()
            path = alias / "child" / LEDGER_FILE_NAME
        else:
            path = alias / LEDGER_FILE_NAME
    calls = []

    def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("不得打开未核实路径")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    result = open_ledger(path)
    assert isinstance(result, LedgerUnavailable)
    assert calls == []
    assert (actual / LEDGER_FILE_NAME).read_bytes() == before


def test_完整原库的已知WAL伴随文件可以核实读取(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path)
    connection = sqlite3.connect(path)
    ledger = None
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE delivery_ledger SET target_message_id='wal-target'")
        connection.commit()
        assert (tmp_path / (LEDGER_FILE_NAME + "-wal")).exists()
        ledger = open_test_ledger(tmp_path)
        assert ledger.get(ledger_key()).target_message_id == "wal-target"
    finally:
        if ledger is not None:
            ledger.close()
        connection.close()


def _committed_wal_snapshot(tmp_path: Path, *, page_size: int = 4096) -> Path:
    """让成功事实仅存在于真实提交的 WAL，再复制主库及伴随文件作为离线输入。"""

    source, snapshot = tmp_path / "source", tmp_path / "snapshot"
    source.mkdir()
    snapshot.mkdir()
    path = source / LEDGER_FILE_NAME
    _legacy_database(path, status="failed", comment_status="", page_size=page_size)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE delivery_ledger SET target_message_id=''")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("UPDATE delivery_ledger SET status='sent', target_message_id='wal-only-target'")
        connection.commit()
        for entry in source.iterdir():
            (snapshot / entry.name).write_bytes(entry.read_bytes())
    finally:
        connection.close()
    return snapshot / LEDGER_FILE_NAME


@pytest.mark.parametrize("byte_order", ["little", "big"])
@pytest.mark.parametrize("page_size", [512, 4096, 65536])
def test_合法WAL两种校验字节序及页面大小保持成功阻断(tmp_path: Path, byte_order: str, page_size: int) -> None:
    """按官方格式转换合成日志；预期成功由 SQLite 自己重放 WAL 验证，不调用待测校验器。"""

    path = _committed_wal_snapshot(tmp_path, page_size=page_size)
    wal = path.with_name(LEDGER_FILE_NAME + "-wal")
    data = bytearray(wal.read_bytes())
    data[:4] = (0x377F0682 if byte_order == "little" else 0x377F0683).to_bytes(4, "big")
    first = second = 0

    def checksum(payload: bytes | bytearray) -> bytes:
        nonlocal first, second
        for index in range(0, len(payload), 8):
            first = (first + int.from_bytes(payload[index:index + 4], byte_order) + second) % 2**32
            second = (second + int.from_bytes(payload[index + 4:index + 8], byte_order) + first) % 2**32
        return first.to_bytes(4, "big") + second.to_bytes(4, "big")

    data[24:32] = checksum(data[:24])
    for offset in range(32, len(data), page_size + 24):
        data[offset + 16:offset + 24] = checksum(data[offset:offset + 8] + data[offset + 24:offset + 24 + page_size])
    wal.write_bytes(data)
    ledger = open_test_ledger(path.parent)
    try:
        record = ledger.get(ledger_key())
        assert (record.status, record.target_message_id) == ("sent", "wal-only-target")
        assert not ledger.reserve(ledger_key()).granted
    finally:
        ledger.close()
    reopened = open_test_ledger(path.parent)
    try:
        assert reopened.get(ledger_key()).target_message_id == "wal-only-target"
        assert not reopened.reserve(ledger_key()).granted
    finally:
        reopened.close()


@pytest.mark.parametrize("damage", ["完整", "头校验", "帧校验", "页内容", "截断", "盐值", "提交标记",
                                    "魔数", "版本", "页大小", "头截断", "帧截断"])
def test_WAL独有成功事实必须核实完整且损坏证据原样保留(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          damage: str) -> None:
    path = _committed_wal_snapshot(tmp_path)
    wal = path.with_name(LEDGER_FILE_NAME + "-wal")
    data = bytearray(wal.read_bytes())
    if damage in {"截断", "头截断", "帧截断"}:
        data = data[:{"截断": -1, "头截断": 31, "帧截断": 55}[damage]]
    elif damage != "完整":
        offset = {"头校验": 24, "帧校验": 48, "页内容": 80, "盐值": 40, "提交标记": 39,
                  "魔数": 0, "版本": 4, "页大小": 8}[damage]
        data[offset] ^= 1
    wal.write_bytes(data)
    before = {entry.name: entry.read_bytes() for entry in path.parent.iterdir()}
    if damage != "完整":
        def must_not_connect(*args: Any, **kwargs: Any) -> None:
            pytest.fail("坏 WAL 必须在 SQLite 触碰主库或伴随证据之前拒绝")
        monkeypatch.setattr(sqlite3, "connect", must_not_connect)
    ledger = open_ledger(path)
    try:
        if damage == "完整":
            assert isinstance(ledger, DeliveryLedger)
            record = ledger.get(ledger_key())
            assert (record.status, record.target_message_id) == ("sent", "wal-only-target")
            assert ledger.reserve(ledger_key()).granted is False
        else:
            assert isinstance(ledger, LedgerUnavailable)
            assert "日志" in ledger.reason and "核实" in ledger.reason
            for name, content in before.items():
                assert (path.parent / name).read_bytes() == content
    finally:
        if isinstance(ledger, DeliveryLedger):
            ledger.close()


@pytest.mark.parametrize("suffix", ["", "-wal"])
def test_WAL核实期间读失败保留证据且不交给SQLite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                suffix: str) -> None:
    path = _committed_wal_snapshot(tmp_path)
    before = {entry.name: entry.read_bytes() for entry in path.parent.iterdir()}
    original_open = Path.open

    def fail_read(file: Path, *args: Any, **kwargs: Any) -> Any:
        if file == path.with_name(LEDGER_FILE_NAME + suffix):
            raise OSError(_SYNTHETIC_PRIVATE)
        return original_open(file, *args, **kwargs)

    def must_not_connect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("WAL 无法核实时不得交给 SQLite 继续恢复")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_read)
        patch.setattr(sqlite3, "connect", must_not_connect)
        ledger = open_ledger(path)
        assert isinstance(ledger, LedgerUnavailable)
        assert _SYNTHETIC_PRIVATE not in ledger.reason
    for name, content in before.items():
        assert (path.parent / name).read_bytes() == content
    # 故障移除即可打开，不能因前次失败丢失 WAL 独有成功事实。
    reopened = open_test_ledger(path.parent)
    try:
        assert reopened.get(ledger_key()).target_message_id == "wal-only-target"
        assert not reopened.reserve(ledger_key()).granted
    finally:
        reopened.close()


# --------------------------------------------------------------------------- 5.4


def test_同键并发只有一次资格且各字段变化相互独立(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ledger = open_test_ledger(tmp_path)
    barrier = Barrier(8)
    key = ledger_key()

    def reserve_together(index: int) -> Reservation:
        barrier.wait(timeout=5)
        return ledger.reserve(key)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            reservations = list(pool.map(reserve_together, range(8)))
        assert sum(item.granted for item in reservations) == 1
        assert all(item.record.status == "sending" for item in reservations)
        for field in ("account_id", "source_group_id", "source_message_id", "target_group_id"):
            changed = replace(key, **{field: "特殊'; DROP TABLE delivery_ledger; --"})
            assert ledger.reserve(changed).granted
            assert ledger.get(changed).key == changed
        assert not ledger.reserve(key).granted
    finally:
        ledger.close()


@pytest.mark.parametrize("status", ["sent", "sending", "unknown", "failed"])
def test_仅明确失败允许新的显式占位(tmp_path: Path, status: str) -> None:
    ledger = open_test_ledger(tmp_path)
    key = ledger_key()
    try:
        ledger.reserve(key)
        if status == "sent":
            ledger.mark_sent(key, "target-known")
        elif status == "unknown":
            ledger.mark_unknown(key, "待核实")
        elif status == "failed":
            ledger.mark_failed(key, "明确未提交")
        second = ledger.reserve(key)
        assert second.granted is (status == "failed")
        assert second.record.status == ("sending" if status == "failed" else status)
    finally:
        ledger.close()


# --------------------------------------------------------------------------- 5.5


def test_重开自动恢复源在途但保留成功未知且不按时间容量清理(tmp_path: Path) -> None:
    clock = FakeClock(start=100)
    ledger = open_test_ledger(tmp_path, clock=clock)
    sent, unknown, sending = (ledger_key(message_id=value) for value in ("sent", "unknown", "sending"))
    try:
        ledger.reserve(sent)
        ledger.mark_sent(sent, "known-target")
        ledger.reserve(unknown)
        ledger.mark_unknown(unknown, "待核实")
        ledger.reserve(sending)
        clock.advance(10 * 365 * 86400)
        for index in range(4100):
            ledger.reserve(ledger_key(message_id=f"other-{index}"))
        assert ledger.get(sent).target_message_id == "known-target"
        assert ledger.get(unknown).status == "unknown"
    finally:
        ledger.close()
    reopened = open_test_ledger(tmp_path, clock=clock)
    try:
        assert reopened.get(sending).status == "unknown"
        assert reopened.get(sent).target_message_id == "known-target"
        assert reopened.get(unknown).status == "unknown"
        assert reopened.recover_interrupted() == 0
        for key in (sent, unknown, sending):
            assert not reopened.reserve(key).granted
        assert reopened.get(ledger_key(message_id="missing")) is None
    finally:
        reopened.close()


@pytest.mark.parametrize("missing_id", ["", "   "])
def test_没有目标ID不能写成成功且原占位保持(tmp_path: Path, missing_id: str) -> None:
    ledger = open_test_ledger(tmp_path)
    try:
        ledger.reserve(ledger_key())
        with pytest.raises(LedgerUnavailableError):
            ledger.mark_sent(ledger_key(), missing_id)
        assert ledger.get(ledger_key()).status == "sending"
    finally:
        ledger.close()


# --------------------------------------------------------------------------- 5.6


@pytest.mark.parametrize("source_status", [None, "sending", "failed", "unknown"])
def test_评论占位仅允许已有源成功(tmp_path: Path, source_status: str | None) -> None:
    ledger = open_test_ledger(tmp_path)
    key = ledger_key()
    try:
        if source_status is not None:
            ledger.reserve(key)
            if source_status == "failed":
                ledger.mark_failed(key, "未发送")
            elif source_status == "unknown":
                ledger.mark_unknown(key, "待核实")
        outcome = ledger.reserve_comment(key)
        assert not outcome.granted
        assert (outcome.record.status if outcome.record else None) == source_status
        assert ledger.record_comment(key, status="sent", message_id="comment") is False
    finally:
        ledger.close()


@pytest.mark.parametrize("terminal", ["sent", "failed", "unknown"])
def test_评论先占位一次再保存终态且禁止覆盖或补发(tmp_path: Path, terminal: LedgerStatus) -> None:
    from concurrent.futures import ThreadPoolExecutor

    ledger = open_test_ledger(tmp_path)
    key = ledger_key()
    try:
        ledger.reserve(key)
        ledger.mark_sent(key, "source-target")
        assert not ledger.record_comment(key, status="sent", message_id="without-reservation")
        with ThreadPoolExecutor(max_workers=8) as pool:
            reservations = list(pool.map(ledger.reserve_comment, [key] * 8))
        assert sum(item.granted for item in reservations) == 1
        assert all(item.record.comment_status == "sending" for item in reservations)
        assert ledger.get(key).comment_status == "sending"
        message_id = "comment-target" if terminal == "sent" else ""
        assert ledger.record_comment(key, status=terminal, message_id=message_id)
        assert not ledger.record_comment(key, status="failed")
        assert not ledger.reserve_comment(key).granted
        assert ledger.get(key).target_message_id == "source-target"
    finally:
        ledger.close()
    reopened = open_test_ledger(tmp_path)
    try:
        record = reopened.get(key)
        assert (record.status, record.comment_status, record.comment_message_id) == ("sent", terminal, message_id)
        assert not reopened.reserve_comment(key).granted
    finally:
        reopened.close()


def test_评论在途重开仅评论转未知并保留源成功(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    key = ledger_key()
    try:
        ledger.reserve(key)
        ledger.mark_sent(key, "source-target")
        assert ledger.reserve_comment(key).granted
    finally:
        ledger.close()
    reopened = open_test_ledger(tmp_path)
    try:
        record = reopened.get(key)
        assert (record.status, record.target_message_id, record.comment_status) == ("sent", "source-target", "unknown")
        assert not reopened.reserve_comment(key).granted
        assert not reopened.record_comment(key, status="sent", message_id="late-receipt")
        assert reopened.recover_interrupted() == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("status,message_id", [("sent", ""), ("sent", "  "), ("sending", ""), ("unexpected", "")])
def test_非法评论终态保留在途占位(tmp_path: Path, status: Any, message_id: str) -> None:
    ledger = open_test_ledger(tmp_path)
    try:
        ledger.reserve(ledger_key())
        ledger.mark_sent(ledger_key(), "source-target")
        ledger.reserve_comment(ledger_key())
        with pytest.raises(LedgerUnavailableError):
            ledger.record_comment(ledger_key(), status=status, message_id=message_id)
        assert ledger.get(ledger_key()).comment_status == "sending"
    finally:
        ledger.close()


# --------------------------------------------------------------------------- 5.7


_SYNTHETIC_PRIVATE = "假源正文|假评论正文|fake-token|fake-resid"


class _FaultCursor(sqlite3.Cursor):
    """在真实 SQLite 存储调用后注入故障，验证实际回滚而非假结果。"""

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        connection = self.connection
        connection.commands.append((sql, tuple(parameters)))
        result = super().execute(sql, parameters)
        phase = sql.split()[0]
        matches = {"begin": {"BEGIN"}, "write": {"INSERT", "UPDATE"}, "query": {"SELECT"}}
        if phase in matches.get(connection.failure, set()):
            connection.failure = ""
            raise sqlite3.OperationalError(_SYNTHETIC_PRIVATE)
        return result

    def close(self) -> None:
        connection = self.connection
        super().close()
        if connection.failure == "cursor_close":
            connection.failure = ""
            raise connection.cursor_error(_SYNTHETIC_PRIVATE)


class _FaultConnection(sqlite3.Connection):
    """通过公开连接工厂注入开始、写入、查询和提交故障。"""

    failure = ""
    rollback_failures = 0
    close_failure = False
    cursor_error = sqlite3.OperationalError

    def rollback(self) -> None:
        if self.rollback_failures:
            self.rollback_failures -= 1
            raise sqlite3.OperationalError(_SYNTHETIC_PRIVATE)
        super().rollback()

    def close(self) -> None:
        if self.close_failure:
            raise sqlite3.OperationalError(_SYNTHETIC_PRIVATE)
        super().close()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if self.failure == "cursor":
            self.failure = ""
            raise self.cursor_error(_SYNTHETIC_PRIVATE)
        return super().cursor(factory=_FaultCursor)

    def commit(self) -> None:
        if self.failure == "commit":
            self.failure = ""
            raise sqlite3.OperationalError(_SYNTHETIC_PRIVATE)
        super().commit()


def _fault_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DeliveryLedger, _FaultConnection]:
    """替换外部存储连接工厂，仍由公开 open_ledger 取得锁、建库和恢复。"""

    connect = sqlite3.connect
    connections: list[_FaultConnection] = []

    def create(*args: Any, **kwargs: Any) -> _FaultConnection:
        connection = connect(*args, **kwargs, factory=_FaultConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", create)
    return open_test_ledger(tmp_path), connections[-1]


@pytest.mark.parametrize("phase", ["cursor", "cursor_close"])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, OSError])
@pytest.mark.parametrize("operation", ["get", "reserve"])
def test_游标创建及关闭故障保持安全错误与真实提交结果(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, error_type: type[Exception], operation: str,
) -> None:
    import traceback

    ledger, connection = _fault_open(tmp_path, monkeypatch)
    key = ledger_key()
    try:
        connection.failure, connection.cursor_error = phase, error_type
        with pytest.raises(LedgerUnavailableError) as caught:
            getattr(ledger, operation)(key)
        assert not connection.in_transaction
        assert _SYNTHETIC_PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert str(caught.value) == ("状态库不可读或所在目录不可写" if error_type is sqlite3.OperationalError else "存储路径不可用")
        record = ledger.get(key)
        if phase == "cursor_close" and operation == "reserve":
            assert record is not None and record.status == STATUS_SENDING
            assert not ledger.reserve(key).granted, "提交后的清理故障不能抹除防重占位"
        else:
            assert record is None
        assert ledger.reserve(ledger_key(message_id="after-cursor-fault")).granted
    finally:
        ledger.close()


@pytest.mark.parametrize("phase", ["begin", "write", "commit"])
@pytest.mark.parametrize("operation", ["reserve", "mark_sent", "mark_failed", "mark_unknown", "reserve_comment",
                                        "record_comment", "recover_interrupted"])
def test_事务任一阶段失败不丢旧记录且连接可继续使用(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   caplog: pytest.LogCaptureFixture, phase: str, operation: str) -> None:
    import traceback

    ledger, connection = _fault_open(tmp_path, monkeypatch)
    old_key, key = ledger_key(message_id="old"), ledger_key()
    try:
        ledger.reserve(old_key)
        ledger.mark_sent(old_key, "old-target")
        if operation != "reserve":
            ledger.reserve(key)
        if operation in {"reserve_comment", "record_comment"}:
            ledger.mark_sent(key, "target")
        if operation == "record_comment":
            ledger.reserve_comment(key)
        before, old_before = ledger.get(key), ledger.get(old_key)
        operations = {
            "reserve": lambda: ledger.reserve(key),
            "mark_sent": lambda: ledger.mark_sent(key, "target"),
            "mark_failed": lambda: ledger.mark_failed(key, "明确未发"),
            "mark_unknown": lambda: ledger.mark_unknown(key, "待核实"),
            "reserve_comment": lambda: ledger.reserve_comment(key),
            "record_comment": lambda: ledger.record_comment(key, status="sent", message_id="comment-target"),
            "recover_interrupted": ledger.recover_interrupted,
        }
        connection.failure = phase
        with pytest.raises(LedgerUnavailableError) as caught:
            operations[operation]()
        assert not connection.in_transaction
        assert ledger.get(key) == before
        assert ledger.get(old_key) == old_before
        assert ledger.reserve(ledger_key(message_id="after-fault")).granted
        assert _SYNTHETIC_PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert _SYNTHETIC_PRIVATE not in caplog.text
    finally:
        ledger.close()


@pytest.mark.parametrize("operation", ["reserve", "reserve_comment"])
@pytest.mark.parametrize("rollback_failures,close_failure", [(1, False), (2, False), (2, True)])
def test_实际写入后回滚失败必须有限恢复或停用并安全释放(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture, operation: str,
                                                    rollback_failures: int, close_failure: bool) -> None:
    import traceback

    ledger, connection = _fault_open(tmp_path, monkeypatch)
    old_key, key = ledger_key(message_id="old-sent"), ledger_key()
    try:
        ledger.reserve(old_key)
        ledger.mark_sent(old_key, "old-target")
        if operation == "reserve_comment":
            ledger.reserve(key)
            ledger.mark_sent(key, "source-target")
        before = ledger.get(key)
        connection.failure = "write"
        connection.rollback_failures = rollback_failures
        connection.close_failure = close_failure
        with pytest.raises(LedgerUnavailableError) as caught:
            getattr(ledger, operation)(key)  # 替身先真实 INSERT/UPDATE，再抛外部存储异常。
        assert _SYNTHETIC_PRIVATE not in "".join(traceback.format_exception(caught.value)) + caplog.text
        if rollback_failures == 1:
            assert not connection.in_transaction
            assert ledger.get(key) == before
            assert ledger.get(old_key).target_message_id == "old-target"
        else:
            with pytest.raises(LedgerUnavailableError, match="停用"):
                ledger.get(old_key)
            with pytest.raises(LedgerUnavailableError, match="停用"):
                ledger.reserve(ledger_key(message_id="after-fault"))
            blocked = open_ledger(tmp_path / LEDGER_FILE_NAME)
            assert isinstance(blocked, LedgerUnavailable)
            if close_failure:
                with pytest.raises(LedgerUnavailableError, match="关闭"):
                    ledger.close()
                assert isinstance(open_ledger(tmp_path / LEDGER_FILE_NAME), LedgerUnavailable)
    finally:
        connection.close_failure = False
        ledger.close()
    reopened = open_test_ledger(tmp_path)
    try:
        assert reopened.get(old_key).target_message_id == "old-target"
        assert not reopened.reserve(old_key).granted
        assert reopened.get(key) == before
        assert reopened.get(ledger_key(message_id="after-fault")) is None
    finally:
        reopened.close()


def test_查询存储故障不是不存在且之后可以继续查询(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, connection = _fault_open(tmp_path, monkeypatch)
    try:
        ledger.reserve(ledger_key())
        expected = ledger.get(ledger_key())
        connection.failure = "query"
        with pytest.raises(LedgerUnavailableError):
            ledger.get(ledger_key())
        assert not connection.in_transaction
        assert ledger.get(ledger_key()) == expected
        assert ledger.get(ledger_key(message_id="missing")) is None
    finally:
        ledger.close()


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_调用方原始原因不能将正文凭证资源写入账本或日志(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        caplog: pytest.LogCaptureFixture, status: str) -> None:
    ledger, connection = _fault_open(tmp_path, monkeypatch)
    try:
        key = ledger_key()
        ledger.reserve(key)
        finalize = ledger.mark_failed if status == "failed" else ledger.mark_unknown
        assert finalize(key, _SYNTHETIC_PRIVATE)
        record = ledger.get(key)
        assert record.status == status and record.reason
        assert _SYNTHETIC_PRIVATE not in repr(record)
        assert _SYNTHETIC_PRIVATE not in repr(connection.commands)
        assert _SYNTHETIC_PRIVATE not in caplog.text
    finally:
        ledger.close()


@pytest.mark.parametrize("phase", ["begin", "write", "commit"])
def test_打开时恢复失败返回不可用并释放独占锁保留原记录(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        phase: str) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path, status="sending", comment_status="")
    before = path.read_bytes()
    connect = sqlite3.connect

    def create(*args: Any, **kwargs: Any) -> _FaultConnection:
        connection = connect(*args, **kwargs, factory=_FaultConnection)
        connection.failure = phase
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", create)
        result = open_ledger(path)
        assert isinstance(result, LedgerUnavailable)
        assert _SYNTHETIC_PRIVATE not in result.reason
    assert path.read_bytes() == before
    reopened = open_test_ledger(tmp_path)
    try:
        assert reopened.get(ledger_key()).status == "unknown"
        assert reopened.get(ledger_key()).target_message_id == "old-target"
    finally:
        reopened.close()


@pytest.mark.parametrize("invalid_schema", [False, True])
def test_打开清理失败安全报告并在依赖恢复后释放独占(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     caplog: pytest.LogCaptureFixture, invalid_schema: bool) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    if invalid_schema:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (id TEXT)")
        connection.close()
    else:
        _legacy_database(path)
    before = path.read_bytes()
    connect = sqlite3.connect
    failing = True

    class ClosingFault(sqlite3.Connection):
        def close(self) -> None:
            if failing:
                raise sqlite3.OperationalError(_SYNTHETIC_PRIVATE)
            super().close()

    def create(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=ClosingFault)

    import fcntl
    import os

    monkeypatch.setattr(sqlite3, "connect", create)
    result = open_ledger(path)
    assert isinstance(result, LedgerUnavailable)
    assert "关闭" in result.reason or "清理" in result.reason
    if invalid_schema:
        assert "缺表" in result.reason  # 清理异常不能掩盖最初的结构诊断。
    assert _SYNTHETIC_PRIVATE not in result.reason + caplog.text
    blocked = open_ledger(path)
    assert isinstance(blocked, LedgerUnavailable)
    descriptor = os.open(tmp_path / "delivery_ledger.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)
    failing = False
    reopened = open_ledger(path)
    if invalid_schema:
        assert isinstance(reopened, LedgerUnavailable)
        assert "缺表" in reopened.reason and "独占" not in reopened.reason
    else:
        assert isinstance(reopened, DeliveryLedger)
        assert reopened.get(ledger_key()).target_message_id == "old-target"
        reopened.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("acquire_then_fail", [False, True])
def test_打开失败且解锁失败保留真实独占直到可以安全清理(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        acquire_then_fail: bool) -> None:
    import fcntl
    import os

    path = tmp_path / LEDGER_FILE_NAME
    if acquire_then_fail:
        _legacy_database(path)
    else:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (id TEXT)")
        connection.close()
    before = path.read_bytes()
    native_flock = fcntl.flock
    failing = True

    def fail_unlock(descriptor: int, operation: int) -> None:
        if failing and operation == fcntl.LOCK_UN:
            raise OSError(_SYNTHETIC_PRIVATE)
        native_flock(descriptor, operation)
        if failing and acquire_then_fail and operation & fcntl.LOCK_EX:
            raise OSError(_SYNTHETIC_PRIVATE)

    monkeypatch.setattr(fcntl, "flock", fail_unlock)
    result = open_ledger(path)
    assert isinstance(result, LedgerUnavailable)
    assert _SYNTHETIC_PRIVATE not in result.reason
    probe = os.open(tmp_path / "delivery_ledger.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            native_flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert isinstance(open_ledger(path), LedgerUnavailable)
        with pytest.raises(BlockingIOError):
            native_flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failing = False
        reopened = open_ledger(path)
        if acquire_then_fail:
            assert isinstance(reopened, DeliveryLedger)
            assert reopened.get(ledger_key()).target_message_id == "old-target"
            reopened.close()
        else:
            assert isinstance(reopened, LedgerUnavailable)
            assert "缺表" in reopened.reason and "独占" not in reopened.reason
        native_flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        native_flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)
    assert path.read_bytes() == before


@pytest.mark.parametrize("closed_first", [False, True])
def test_锁描述符关闭失败不泄密不误关复用描述符且独占可接管(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          closed_first: bool) -> None:
    import os
    import traceback

    directory = tmp_path / "plugin"
    directory.mkdir()
    ledger = open_test_ledger(directory)
    ledger.reserve(ledger_key())
    ledger.mark_sent(ledger_key(), "known-target")
    original_close = os.close
    retained: list[int] = []

    def fail_close(descriptor: int) -> None:
        if closed_first:
            original_close(descriptor)
            other = os.open(tmp_path / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
            assert other == descriptor  # 明确制造句柄号码复用；旧账本不得再次 close 它。
            retained.append(other)
        else:
            retained.append(descriptor)
        raise OSError(_SYNTHETIC_PRIVATE)

    second = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", fail_close)
            with pytest.raises(LedgerUnavailableError) as caught:
                ledger.close()
        assert _SYNTHETIC_PRIVATE not in "".join(traceback.format_exception(caught.value))
        with pytest.raises(LedgerUnavailableError, match="关闭未确认"):
            ledger.close()
        os.fstat(retained[0])
        second = open_ledger(directory / LEDGER_FILE_NAME)
        assert isinstance(second, DeliveryLedger)
        assert second.get(ledger_key()).target_message_id == "known-target"
        with pytest.raises(LedgerUnavailableError):
            ledger.close()
        blocked = open_ledger(directory / LEDGER_FILE_NAME)
        assert isinstance(blocked, LedgerUnavailable)
    finally:
        if isinstance(second, DeliveryLedger):
            second.close()
        for descriptor in retained:
            original_close(descriptor)


# --------------------------------------------------------------------------- 4.1


def test_账本键恰为账号源群源消息与目标群且不含评论(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    key = ledger_key()

    reservation = ledger.reserve(key)
    assert isinstance(reservation, Reservation)
    assert reservation.granted is True
    assert reservation.record.status == STATUS_SENDING
    assert reservation.record.key == key

    for label, other in (
        ("账号", replace(key, account_id="99998")),
        ("源群", replace(key, source_group_id="10555")),
        ("源消息", replace(key, source_message_id="m-other")),
        ("目标群", replace(key, target_group_id="20003")),
    ):
        assert ledger.get(other) is None, f"{label}不同即为不同键，不应命中同一条记录"

    assert ledger.mark_sent(key, "delivered-9") is True
    assert ledger.reserve_comment(key).granted is True
    assert ledger.record_comment(key, status=STATUS_SENT, message_id="comment-7") is True
    record = ledger.get(key)
    assert record is not None and record.comment_message_id == "comment-7", (
        "评论不参与键：写入评论后仍按原键读回，不需要新键"
    )
    ledger.close()


def test_账本区分四种状态并保存评论结果(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    sending = ledger_key(message_id="m-sending")
    sent = ledger_key(message_id="m-sent")
    failed = ledger_key(message_id="m-failed")
    unknown = ledger_key(message_id="m-unknown")

    assert ledger.reserve(sending).record.status == STATUS_SENDING

    assert ledger.reserve(sent).granted is True
    assert ledger.mark_sent(sent, "delivered-9") is True
    assert ledger.reserve_comment(sent).granted is True
    assert ledger.record_comment(sent, status=STATUS_SENT, message_id="comment-7") is True

    assert ledger.reserve(failed).granted is True
    assert ledger.mark_failed(failed, "明确未发送") is True

    assert ledger.reserve(unknown).granted is True
    assert ledger.mark_unknown(unknown, "回执丢失") is True

    record = ledger.get(sent)
    assert record is not None
    assert record.status == STATUS_SENT
    assert record.target_message_id == "delivered-9", "成功记录必须保存真实目标消息 ID"
    assert record.comment_status == STATUS_SENT, "评论结果附属于源发送记录"
    assert record.comment_message_id == "comment-7"
    assert ledger.get(failed).status == STATUS_FAILED
    assert ledger.get(unknown).status == STATUS_UNKNOWN

    ledger.close()
    reopened = open_test_ledger(tmp_path)
    again = reopened.get(sent)
    assert again is not None
    assert (again.status, again.target_message_id) == (STATUS_SENT, "delivered-9")
    assert (again.comment_status, again.comment_message_id) == (STATUS_SENT, "comment-7"), (
        "评论结果必须跨重开账本保留"
    )
    assert reopened.get(failed).status == STATUS_FAILED
    assert reopened.get(unknown).status == STATUS_UNKNOWN
    reopened.close()


def test_SQL参数绑定且特殊字符ID按字面量存取(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    hostile = "m-1'; DROP TABLE delivery_ledger; --"
    key = ledger_key(message_id=hostile)

    assert ledger.reserve(key).granted is True
    assert ledger.mark_sent(key, "delivered-'; DROP TABLE delivery_ledger; --") is True

    record = ledger.get(key)
    assert record is not None
    assert record.key.source_message_id == hostile, "特殊字符必须按字面量存取（参数绑定）"
    assert record.target_message_id == "delivered-'; DROP TABLE delivery_ledger; --"

    other = ledger.reserve(ledger_key(message_id="m-2"))
    assert other.granted is True, "表结构必须完好，注入字符串不得被当作 SQL 执行"
    ledger.close()


# --------------------------------------------------------------- 事务边界与失败可区分


class _FlakyCommitConnection(sqlite3.Connection):
    """可注入一次提交失败的连接：用真实 sqlite 连接表达存储故障，不伪造账本接口。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fail_next_commit = False

    def fail_next_commit(self) -> None:
        """让下一次 `commit()` 抛错（不写入任何数据）。"""

        self._fail_next_commit = True

    def commit(self) -> None:
        if self._fail_next_commit:
            self._fail_next_commit = False
            raise sqlite3.OperationalError("注入的提交失败")
        super().commit()


def _flaky_ledger(tmp_path: Path) -> tuple[DeliveryLedger, _FlakyCommitConnection]:
    """先建好表，再用可注入提交失败的连接构造账本。"""

    path = tmp_path / LEDGER_FILE_NAME
    open_test_ledger(tmp_path).close()
    connection = sqlite3.connect(
        str(path),
        timeout=5.0,
        isolation_level=None,
        factory=_FlakyCommitConnection,
    )
    return DeliveryLedger(connection, FakeClock()), connection


def test_提交失败后回滚且连接仍可继续使用(tmp_path: Path) -> None:
    ledger, connection = _flaky_ledger(tmp_path)
    try:
        connection.fail_next_commit()
        with pytest.raises(LedgerUnavailableError) as failure:
            ledger.reserve(ledger_key(message_id="m-commit-fail"))

        assert str(failure.value).strip(), "提交失败必须如实报告不可用"

        resumed = ledger.reserve(ledger_key(message_id="m-after-commit-fail"))
        assert isinstance(resumed, Reservation) and resumed.granted is True, (
            f"提交失败必须回滚，连接不得停留在未结束事务: {resumed!r}"
        )
        assert ledger.get(ledger_key(message_id="m-after-commit-fail")) is not None
    finally:
        ledger.close()


def test_记录不存在与读取失败可区分(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)

    assert ledger.get(ledger_key(message_id="m-missing")) is None, "记录不存在必须返回 None"

    ledger.close()
    with pytest.raises(LedgerUnavailableError) as failure:
        ledger.get(ledger_key())
    assert str(failure.value).strip(), "读取失败必须给出可定位原因，不能与「不存在」混同"


def test_写入失败与占位未命中可区分(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)

    assert ledger.mark_sent(ledger_key(message_id="m-absent"), "m-delivered-x") is False, (
        "没有可更新的在途占位时返回 False（本次未登记），不得与本条失败语义混同"
    )

    ledger.close()
    with pytest.raises(LedgerUnavailableError) as failure:
        ledger.mark_sent(ledger_key(), "m-delivered-x")
    assert str(failure.value).strip(), "存储失败必须抛 LedgerUnavailableError，不能静默返回 False"


def test_恢复失败与零条可区分(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    assert ledger.recover_interrupted() == 0, "空账本恢复零条"

    ledger.close()
    with pytest.raises(LedgerUnavailableError):
        ledger.recover_interrupted()


def test_发送状态与账本状态共用同一值域声明() -> None:
    """发送步骤的结果值域不得另行声明一份字面量（第 4 组评审遗留 #3）。

    `SendStatus` 必须是 `LedgerStatus` 本身（同一类型对象），而不是复制的
    `Literal[...]`：两份声明一旦漂移，编排的兜底分支与账本状态就会不一致。
    """

    from forward_delivery import SendStatus

    assert SendStatus is LedgerStatus, "发送状态值域必须与账本状态共用同一声明"


# --------------------------------------------------------------------------- 4.6


def test_损坏账本返回不可用且不覆盖原文件(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    path.write_bytes("这不是一个 SQLite 数据库".encode("utf-8") + b"\x00\xff" * 8)
    broken_bytes = path.read_bytes()

    outcome = open_ledger(path, clock=FakeClock())

    assert isinstance(outcome, LedgerUnavailable), f"损坏库必须返回不可用: {outcome!r}"
    assert "损坏" in outcome.reason, outcome
    assert path.exists() and path.read_bytes() == broken_bytes, (
        "不得删除、覆盖或改写损坏的状态数据"
    )


# --------------------------------------------------------------------------- 9.1
# 真实部署中宿主把 `ctx.paths.data_dir` 指向插件目录，目录里必然同时存在源码、
# 文档、配置与缓存（design D11）。历史核对只阻断状态类证据，部署文件不阻断发送。

_DEPLOYMENT_FILE_CONTENT = b"synthetic-deployment-file"


def _deployment_directory(directory: Path) -> None:
    """按真实部署形态写入插件自身文件、缓存与配置备份；全部不是状态类证据。"""

    for name in ("plugin.py", "config.toml", "_manifest.json", "README.md", "pyrightconfig.json",
                 "config.toml.backup.20260929233435"):
        (directory / name).write_bytes(_DEPLOYMENT_FILE_CONTENT)
    cache = directory / "__pycache__"
    cache.mkdir()
    (cache / "delivery_ledger.cpython-313.pyc").write_bytes(b"\x00synthetic-pyc")
    (cache / "x.pyc").write_bytes(b"\x00synthetic-pyc")
    backup = directory / "config_back"
    backup.mkdir()
    (backup / "config.toml").write_bytes(b"synthetic-config-backup")


def test_状态目录内的部署文件不阻断且既有成功记录仍阻止重发(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path)
    _deployment_directory(tmp_path)
    before = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir() if entry.is_file()}
    ledger = open_test_ledger(tmp_path)
    try:
        record = ledger.get(ledger_key())
        assert (record.status, record.target_message_id) == ("sent", "old-target")
        assert not ledger.reserve(ledger_key()).granted
        assert ledger.reserve(ledger_key(message_id="m-after-deploy")).granted
    finally:
        ledger.close()
    del before[LEDGER_FILE_NAME]  # 本次确有合法写入（占位与评论恢复），只核部署文件不被改动。
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content, f"部署文件不得因打开账本被改动: {name}"


def test_仅有部署文件且无主库时按新装建库(tmp_path: Path) -> None:
    _deployment_directory(tmp_path)
    ledger = open_test_ledger(tmp_path)
    try:
        assert ledger.get(ledger_key()) is None
        assert ledger.reserve(ledger_key()).granted
    finally:
        ledger.close()


@pytest.mark.parametrize("name", ["config.toml.backup.20260929233435", "__pycache__/x.pyc"])
def test_数字时间戳备份名与缓存文件名仍按部署文件放行(tmp_path: Path, name: str) -> None:
    """反例：ASCII `backup` 不进标记集，数字时间戳扩展名与 `.pyc` 缓存都不是证据。

    `config.toml.backup.20260929233435` 的扩展名是数字时间戳，`config_back/` 的
    `back` 也不得按 ASCII `backup` 子串命中；否则真实部署会被误阻断。
    """

    _deployment_directory(tmp_path)
    target = tmp_path / name
    target.write_bytes(_DEPLOYMENT_FILE_CONTENT)
    ledger = open_test_ledger(tmp_path)
    try:
        assert ledger.reserve(ledger_key()).granted
    finally:
        ledger.close()
    assert target.read_bytes() == _DEPLOYMENT_FILE_CONTENT


def test_纯任意名文件无法按名字判定为证据(tmp_path: Path) -> None:
    """固有边界（已在 verification/09 记为残留风险）：无扩展名、无状态标记、非
    `.tmp-8hex` 的名字无法按名字识别，只能靠人工核对；名字判定不假装已覆盖内容。
    """

    _deployment_directory(tmp_path)
    marker = tmp_path / "x"
    marker.write_bytes(b"synthetic-unidentified-file")
    ledger = open_test_ledger(tmp_path)
    try:
        assert ledger.reserve(ledger_key()).granted
    finally:
        ledger.close()
    assert marker.read_bytes() == b"synthetic-unidentified-file"


@pytest.mark.parametrize("evidence", [
    "service_binding.json",
    "service_binding.json.tmp",
    "Service_Binding.JSON",
    "service_ledger.sqlite3",
    "service.lock",
    "delivery_ledger.sqlite3.bak",
    "delivery_ledger-20261001.sqlite3",
    "snapshot-20261001.tar",
    "ledger_export.tmp-1234abcd",
    "export.tmp-1234abcd",
    "export.tmp-1234abcd\n",
    "备份.tar.gz",
    "人工快照.tar",
    "导出归档",
])
def test_状态类证据仍阻断且不打开数据库也不改动文件(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str,
) -> None:
    _deployment_directory(tmp_path)
    marker = tmp_path / evidence
    marker.write_bytes(b"\xffsynthetic-state-evidence")
    before = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir() if entry.is_file()}
    calls = []

    def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("状态类证据未人工核对前不得打开任何数据库")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    assert isinstance(result, LedgerUnavailable)
    assert calls == []
    assert "synthetic-state-evidence" not in result.reason
    assert not (tmp_path / LEDGER_FILE_NAME).exists()
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_部署文件不能把孤立伴随文件当新装(tmp_path: Path, suffix: str) -> None:
    _deployment_directory(tmp_path)
    marker = tmp_path / (LEDGER_FILE_NAME + suffix)
    marker.write_bytes(b"synthetic-orphan-companion")
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    assert isinstance(result, LedgerUnavailable)
    assert not (tmp_path / LEDGER_FILE_NAME).exists()
    assert marker.read_bytes() == b"synthetic-orphan-companion"


@pytest.mark.parametrize("name", ["delivery_ledger.sqlite3.bak", "service_ledger.sqlite3", "ledger.sqlite3-wal"])
def test_子目录一层名字命中状态规则即阻断且不改动(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    _deployment_directory(tmp_path)
    # 目录自身不带任何标记，专门验证「只检查一层子项名字」这一条仍生效。
    directory = tmp_path / "插件子目录"
    directory.mkdir()
    (directory / name).write_bytes(b"\xffsynthetic-child-evidence")
    calls = []

    def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("子目录状态证据未人工核对前不得打开数据库")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    assert isinstance(result, LedgerUnavailable)
    assert calls == []
    assert "synthetic-child-evidence" not in result.reason
    assert (directory / name).read_bytes() == b"\xffsynthetic-child-evidence"


@pytest.mark.parametrize("name", ["snapshot-export", "delivery_backup"])
def test_子目录自身名字命中状态标记即阻断(tmp_path: Path, name: str) -> None:
    _deployment_directory(tmp_path)
    directory = tmp_path / name
    directory.mkdir()
    (directory / "内容.py").write_bytes(b"synthetic-deep-content")
    result = open_ledger(tmp_path / LEDGER_FILE_NAME)
    assert isinstance(result, LedgerUnavailable)
    assert (directory / "内容.py").read_bytes() == b"synthetic-deep-content"
