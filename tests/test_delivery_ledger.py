"""账本行为：经持久状态模块的**导出接口**驱动，不测试私有 SQL 辅助函数。

用例只断言账本对外承诺的行为：五字段键构成与旧四字段库一次性迁移、占位与终态
的防重纪律、评论占位与结果保存、重开后的在途恢复、失败与「记录不存在」的可区
分性，以及路径证据、损坏库与 WAL 独有成功事实的安全关闭。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from maimai_courier_plugin.shared.delivery import (
    STATUS_SENDING,
    STATUS_SENT,
    TARGET_KIND_GROUP,
    TARGET_KIND_PRIVATE,
    LedgerStatus,
    LedgerUnavailable,
    LedgerUnavailableError,
)
from maimai_courier_plugin.delivery_ledger import (
    LEDGER_FILE_NAME,
    DeliveryLedger,
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

CURRENT_SCHEMA = LEGACY_SCHEMA.replace(
    "target_group_id TEXT NOT NULL,",
    "target_group_id TEXT NOT NULL, target_kind TEXT NOT NULL,",
).replace(
    "PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)",
    "PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id, target_kind)",
)


def _legacy_database(path: Path, *, schema: str = LEGACY_SCHEMA, kind: str | None = None,
                     status: str = "sent", comment_status: str = "sent", page_size: int = 4096) -> None:

    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA page_size={page_size}")
        connection.execute(schema)
        values: tuple[Any, ...] = (
            "10001", "111", "m-source-1", "222", *(() if kind is None else (kind,)),
            status, "old-target", comment_status, "old-comment" if comment_status else "",
            "旧安全原因", 10.0, 20.0,
        )
        connection.execute(
            f"INSERT INTO delivery_ledger VALUES ({', '.join('?' * len(values))})", values
        )
        connection.commit()
    finally:
        connection.close()


def test_五字段新库可直接打开且二次打开不重复迁移(tmp_path: Path) -> None:
    """当前五字段库（含目标形态列）可直接打开；再次打开不重建表、事实保留。"""

    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path, schema=CURRENT_SCHEMA, kind=TARGET_KIND_GROUP)
    opened = open_test_ledger(tmp_path)
    try:
        record = opened.get(ledger_key())
        assert record is not None
        assert (record.status, record.target_message_id,
                record.comment_status, record.comment_message_id) == (
                    "sent", "old-target", "sent", "old-comment")
    finally:
        opened.close()
    after_first = path.read_bytes()
    reopened = open_test_ledger(tmp_path)
    try:
        again = reopened.get(ledger_key())
        assert again is not None
        assert (again.status, again.target_message_id,
                again.comment_status, again.comment_message_id) == (
                    "sent", "old-target", "sent", "old-comment")
    finally:
        reopened.close()
    assert path.read_bytes() == after_first, "五字段库再次打开不得重建表"


def test_旧四字段库与五字段新库都是认可形状(tmp_path: Path) -> None:
    """两种形状都能打开：旧四字段库记录原样可读，五字段新库直接可用。"""

    for name, schema, kind in (("legacy", LEGACY_SCHEMA, None), ("current", CURRENT_SCHEMA, TARGET_KIND_GROUP)):
        directory = tmp_path / name
        directory.mkdir()
        _legacy_database(directory / LEDGER_FILE_NAME, schema=schema, kind=kind)
        ledger = open_test_ledger(directory)
        try:
            record = ledger.get(ledger_key())
            assert record is not None, name
            assert (record.status, record.target_message_id) == ("sent", "old-target"), name
        finally:
            ledger.close()


def test_旧四字段库一次性迁移后记录逐条保留且继续阻断重发(tmp_path: Path) -> None:
    """旧四字段库打开即一次性迁移：记录数、状态与 `target_message_id` 逐条一致。

    期待值独立来自构造时写入的每一行；评论在途行按既有恢复口径转未知，源事实不变。
    """

    path = tmp_path / LEDGER_FILE_NAME
    connection = sqlite3.connect(path)
    try:
        connection.execute(LEGACY_SCHEMA)
        connection.executemany(
            "INSERT INTO delivery_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("10001", "111", "m-sent", "222", "sent", "target-sent", "sent", "comment-sent", "", 10.0, 20.0),
                ("10001", "111", "m-unknown", "222", "unknown", "", "", "", "", 11.0, 21.0),
                ("10001", "111", "m-negative", "30003", "sent", "-1289506878", "", "", "", 12.0, 22.0),
                ("10001", "111", "m-failed", "222", "failed", "", "", "", "明确未发送", 13.0, 23.0),
                ("10001", "111", "m-comment-sending", "30003", "sent", "target-pending", "sending", "", "", 14.0, 24.0),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    expected = [
        (ledger_key(message_id="m-sent"), "sent", "target-sent", "sent", "comment-sent", "", 10.0, 20.0),
        (ledger_key(message_id="m-unknown"), "unknown", "", "", "", "", 11.0, 21.0),
        (ledger_key(message_id="m-negative", target_group_id="30003"), "sent", "-1289506878", "", "", "", 12.0, 22.0),
        (ledger_key(message_id="m-failed"), "failed", "", "", "", "明确未发送", 13.0, 23.0),
        (ledger_key(message_id="m-comment-sending", target_group_id="30003"),
         "sent", "target-pending", "unknown", "", "", 14.0, 0.0),
    ]
    ledger = open_test_ledger(tmp_path)
    try:
        for key, status, target_message_id, comment_status, comment_message_id, reason, created, updated in expected:
            record = ledger.get(key)
            assert record is not None, key
            assert (record.status, record.target_message_id, record.comment_status,
                    record.comment_message_id, record.reason, record.created_at,
                    record.updated_at) == (status, target_message_id, comment_status,
                                           comment_message_id, reason, created, updated), key
    finally:
        ledger.close()
    after = path.read_bytes()
    reopened = open_test_ledger(tmp_path)
    try:
        for key, status, target_message_id, *_ in expected:
            again = reopened.get(key)
            assert again is not None
            assert (again.status, again.target_message_id) == (status, target_message_id), key
    finally:
        reopened.close()
    assert path.read_bytes() == after, "一次性迁移：二次打开不得再改写文件"
    # 既有键的占位阻断（`reserve` 对迁移后的记录仍不授予）随五字段键写入路径一并断言，
    # 见 test_同号群与私聊互不阻断且评论占位只为已有源记录创建。


@pytest.mark.parametrize("phase", ["校验", "写", "提交"])
def test_迁移任一步失败保留原文件并按账本不可用关闭发送(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    """注入校验失败/写盘失败/提交失败：原文件字节不变，按账本不可用关闭发送。

    形状不可识别（未知结构）在迁移前的只读验证即被拒绝，同样不改写原文件，由
    test_结构或状态不兼容不当空库修复 与 test_目标形态相关但结构不符 覆盖。
    """

    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path)
    before = path.read_bytes()
    connect = sqlite3.connect
    faults = {"校验": None, "写": "write", "提交": "commit"}

    def create(*args: Any, **kwargs: Any) -> _FaultConnection:
        connection = connect(*args, **kwargs, factory=_FaultConnection)
        if faults[phase] is None:
            connection.skip_inserts = True  # 回填被跳过：记录数校验必须失败。
        else:
            connection.failure = faults[phase]  # 写：INSERT 后抛；提交：COMMIT 前抛。
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", create)
        result = open_ledger(path)
        assert isinstance(result, LedgerUnavailable)
        assert "迁移" in result.reason, result.reason
        assert _SYNTHETIC_PRIVATE not in result.reason
    assert path.read_bytes() == before, "迁移失败必须保留原文件字节，不新建空库、不回迁"
    reopened = open_test_ledger(tmp_path)
    try:
        assert reopened.get(ledger_key()).target_message_id == "old-target"
    finally:
        reopened.close()


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
                                      "旧账本快照.sqlite3"])
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
    # 诊断只含安全条目名与类别，不回显条目正文。
    assert "假敏感正文" not in result.reason and "damaged-or-secret" not in result.reason
    assert (path.read_bytes() if existing else None) == before
    assert path.exists() is existing
    if marker.is_file():
        assert marker.read_bytes() == b"\xff damaged-or-secret"
    else:
        assert (marker / "delivery_ledger.sqlite3.bak").read_text() == "假敏感正文"


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


def _committed_wal_snapshot(tmp_path: Path, *, page_size: int = 4096) -> Path:

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


@pytest.mark.parametrize("damage", ["完整", "头校验", "帧校验", "页内容", "截断", "盐值"])
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


# --------------------------------------------------------------------------- 5.7


_SYNTHETIC_PRIVATE = "假源正文|假评论正文|fake-token|fake-resid"


class _FaultCursor(sqlite3.Cursor):
    """在真实 SQLite 存储调用后注入故障，验证实际回滚而非假结果。"""

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        connection = self.connection
        connection.commands.append((sql, tuple(parameters)))
        if connection.skip_inserts and sql.split()[0] == "INSERT":
            return self  # 迁移回填被跳过：新表为空、旧表有行，记录数校验必须失败。
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
    skip_inserts = False
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


# --------------------------------------------------------------------------- 4.1


def test_同号群与私聊互不阻断且评论占位只为已有源记录创建(tmp_path: Path) -> None:
    """五字段键：群 12345 与好友 12345 是两笔独立投递，占位/落账/评论互不覆盖。"""

    ledger = open_test_ledger(tmp_path)
    group, private = ledger_key(), replace(ledger_key(), target_kind=TARGET_KIND_PRIVATE)
    try:
        assert ledger.reserve(group).granted
        assert ledger.get(private) is None, "同号私聊目标不得命中群记录"
        assert ledger.reserve(private).granted, "群占位不得阻断同号私聊占位"
        assert ledger.mark_sent(group, "group-target") is True
        assert ledger.mark_sent(private, "private-target") is True
        assert ledger.get(group).target_message_id == "group-target"
        assert ledger.get(private).target_message_id == "private-target"
        assert not ledger.reserve(group).granted and not ledger.reserve(private).granted
        # 评论占位按同一五字段键：各自只为自己的已成功源记录授予一次。
        assert ledger.reserve_comment(group).granted
        assert not ledger.reserve_comment(group).granted
        assert ledger.reserve_comment(private).granted
        assert ledger.record_comment(group, status=STATUS_SENT, message_id="group-comment")
        assert ledger.record_comment(private, status=STATUS_SENT, message_id="private-comment")
        assert ledger.get(group).comment_message_id == "group-comment"
        assert ledger.get(private).comment_message_id == "private-comment"
        # 不存在的源记录：评论占位不创建记录（返回 None 而非抛存储故障）。
        missing = replace(private, target_group_id="99999")
        assert ledger.reserve_comment(missing) == Reservation(False, None)
    finally:
        ledger.close()
    reopened = open_test_ledger(tmp_path)
    try:
        assert reopened.get(group).comment_message_id == "group-comment"
        assert reopened.get(private).comment_message_id == "private-comment"
        assert not reopened.reserve(group).granted and not reopened.reserve(private).granted
    finally:
        reopened.close()


def test_迁移后的既有键仍阻断重发且同号私聊可独立投递(tmp_path: Path) -> None:
    """迁移保留阻断作用；既有群记录的同一目标号码可按私聊形态独立占位。"""

    path = tmp_path / LEDGER_FILE_NAME
    _legacy_database(path)
    group = ledger_key()
    private = replace(group, target_kind=TARGET_KIND_PRIVATE)
    ledger = open_test_ledger(tmp_path)
    try:
        assert not ledger.reserve(group).granted, "迁移后的既有成功事实继续阻断重发"
        assert ledger.get(private) is None
        assert ledger.reserve(private).granted
        assert not ledger.reserve_comment(group).granted, "既有评论已终态，不重复授予"
        assert not ledger.reserve_comment(private).granted, "评论占位只对已成功源记录授予"
        assert ledger.mark_sent(private, "private-target") is True
    finally:
        ledger.close()
    reopened = open_test_ledger(tmp_path)
    try:
        assert reopened.get(group).target_message_id == "old-target"
        assert reopened.get(private).target_message_id == "private-target"
    finally:
        reopened.close()


def test_账本键恰为账号源群源消息目标与目标形态且不含评论(tmp_path: Path) -> None:
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
        ("目标形态", replace(key, target_kind=TARGET_KIND_PRIVATE)),
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


def test_负数目标消息ID按原值存取且重开保留(tmp_path: Path) -> None:

    ledger = open_test_ledger(tmp_path)
    key = ledger_key(message_id="m-negative-target")
    assert ledger.reserve(key).granted is True
    assert ledger.mark_sent(key, "-1289506878") is True

    record = ledger.get(key)
    assert record is not None
    assert (record.status, record.target_message_id) == (STATUS_SENT, "-1289506878"), (
        "真实目标消息 ID 按返回原值存取，负号不是拒绝或改写理由"
    )
    ledger.close()

    reopened = open_test_ledger(tmp_path)
    again = reopened.get(key)
    assert again is not None and again.target_message_id == "-1289506878", "负数 ID 跨重开保留"
    reopened.close()


# --------------------------------------------------------------------------- 事务边界与失败可区分


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


def test_发送状态与账本状态共用同一值域声明() -> None:

    from maimai_courier_plugin.forward_delivery import SendStatus

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
# 文档、配置与缓存。历史核对只阻断状态类证据，部署文件不阻断发送。

_DEPLOYMENT_FILE_CONTENT = b"synthetic-deployment-file"


def _deployment_directory(directory: Path) -> None:

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


@pytest.mark.parametrize("evidence", [
    "service_binding.json",
    "service_ledger.sqlite3",
    "delivery_ledger.sqlite3.bak",
    "snapshot-20261001.tar",
    "ledger_export.tmp-1234abcd",
    "export.tmp-1234abcd\n",
    "备份.tar.gz",
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
